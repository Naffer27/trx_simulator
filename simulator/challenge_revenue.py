# simulator/challenge_revenue.py
"""
simulator/challenge_revenue.py — CHALLENGE-REVENUE-WRITER-01 (FASE B).

CORE SERVICE for BrokerLedger.REV_CHALLENGE_FEE. The one and only
sanctioned entry point that may create a REV_CHALLENGE_FEE row.

Records a real economic event that has ALREADY occurred — payment
already verified sufficient by one of the three certified purchase
flows (NowPayments crypto callback, internal Wallet purchase, external
sales platform webhook — see "BROKER-ECONOMICS — Challenge Revenue
Writer" FASE A). This function does not verify payment, does not
create the commercial fact it records, and does not decide whether a
challenge purchase happened — it trusts its caller entirely, exactly
as broker_economic_adjustment.create_broker_economic_adjustment()
trusts its own caller for authorization. Call it ONLY immediately
after ChallengeEnrollment.objects.create() has succeeded, inside the
same atomic transaction as that create() call, from one of the three
certified call sites in views.py — never from
activate_challenge_enrollment() (shared by admin re-activation
actions) and never from any admin action. Zero sweep, zero cron, zero
Celery task, zero signal: this module has exactly one way to run, a
direct call from already-verified purchase-flow code.

Amount: enrollment.product.price_usd — read live from the product FK
at write time, never cached or pre-computed. This is GROSS challenge
revenue, not net profit; no discount/coupon/payment-processing-cost/
tax field exists anywhere in this codebase to subtract (confirmed by
audit in FASE A), and this module invents none. Retained Broker
Economics (broker_economics_summary.py) correctly continues reporting
PARTIAL coverage on its cost side — this writer does not change that.

Idempotency: primarily DB-enforced via BrokerLedger's own
UniqueConstraint(source_challenge_enrollment, revenue_type) (migration
0089_challenge_fee_ledger_link) — at most one REV_CHALLENGE_FEE row can
ever exist per ChallengeEnrollment, regardless of retries, duplicate
webhooks, or concurrent execution. This function raises a typed
exception (DuplicateChallengeRevenue) when that constraint is hit,
rather than leaking a raw IntegrityError, so callers already handling
other purchase-flow exceptions can react uniformly. It does not use
exists()/get_or_create() as its primary protection — under a true
concurrent race, only a DB-level unique constraint is authoritative;
an application-level existence check is not.

Transactional integrity: the write happens inside its own
transaction.atomic() block, which — per Django's re-entrant atomic()
semantics — becomes a SAVEPOINT (not a new independent transaction)
when called from inside an already-open outer atomic() block, exactly
as wallet_ledger.credit_wallet()/debit_wallet() already document for
themselves. Every certified call site opens its own outer atomic()
before calling this function, so if any later step in that same
transaction fails and the whole thing rolls back, the REV_CHALLENGE_FEE
row rolls back with it — it can never be left orphaned. Calling this
function with no open outer transaction is unsupported for production
use: it would commit independently.

Isolation, by construction, not by policy: this module imports nothing
from wallet_ledger, ib_treasury_settlement, ib_commission,
ib_commission_triggers, or any Treasury/trading-engine module. Calling
record_challenge_fee_revenue() can only ever write a single
BrokerLedger row — never a TradingAccount, Wallet, Deposit,
WithdrawalRequest, TreasuryOperationRequest, or IBCommissionObligation.

Not for backfill: there is no batch/bulk entry point. One call is one
deliberate record of one already-verified payment, made by the caller
that verified it. BrokerLedger.created_at is auto_now_add — every row
this module ever creates is stamped with the real time it was created,
never a historical timestamp.
"""
from django.db import IntegrityError, transaction

from .models import BrokerLedger, ChallengeEnrollment


class DuplicateChallengeRevenue(Exception):
    """Raised when a REV_CHALLENGE_FEE row already exists for this enrollment.

    Signals that BrokerLedger's DB-level UniqueConstraint
    (source_challenge_enrollment, revenue_type) rejected a second write
    for the same ChallengeEnrollment — a retry, a duplicate webhook
    delivery, or a genuine concurrent race against this exact
    enrollment. Never silently swallowed by this module; the caller
    decides how to react (typically: log at warning level and
    continue — the revenue is already correctly booked).
    """


def record_challenge_fee_revenue(enrollment: ChallengeEnrollment) -> BrokerLedger:
    """
    Record GROSS challenge revenue for one already-created ChallengeEnrollment.

    Must be called inside the same transaction.atomic() block that just
    created *enrollment*, immediately after ChallengeEnrollment.objects.create()
    succeeded. Raises DuplicateChallengeRevenue if this enrollment already
    has a REV_CHALLENGE_FEE row — safe under concurrency, backed by a DB
    unique constraint, not an application-level check.

    Returns the created BrokerLedger row.
    """
    if enrollment.pk is None:
        raise ValueError(
            "record_challenge_fee_revenue: enrollment must already be saved (pk is None)"
        )

    amount = enrollment.product.price_usd

    try:
        with transaction.atomic():
            return BrokerLedger.objects.create(
                revenue_type=BrokerLedger.REV_CHALLENGE_FEE,
                amount=amount,
                source_challenge_enrollment=enrollment,
                meta={
                    "enrollment_id": enrollment.pk,
                    "product_id": enrollment.product_id,
                    "product_tier": enrollment.product.tier,
                    "user_id": enrollment.user_id,
                },
            )
    except IntegrityError as exc:
        raise DuplicateChallengeRevenue(
            f"REV_CHALLENGE_FEE already recorded for enrollment #{enrollment.pk}"
        ) from exc
