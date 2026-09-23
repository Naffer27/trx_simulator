"""
simulator/broker_economic_adjustment.py — BROKER-ECONOMICS-02B (FASE B).

CORE SERVICE for BrokerEconomicAdjustment / BrokerLedger.REV_ADJUSTMENT.
The one and only sanctioned entry point that may create a REV_ADJUSTMENT
BrokerLedger row.

Deliberately carries NO permission, authentication, or authorization
logic — no is_owner_root() check, no TOTP verification, no
reauthentication. That belongs to a future Owner Control Plane block
(BROKER-ECONOMICS-02C), which will call create_broker_economic_adjustment()
below only after its own gate has already passed. This module must never
import from, or depend on, simulator.owner_actions — the dependency runs
the other way: a future 02C function in owner_actions.py will import and
call this module, never the reverse.

actor/reason/idempotency_key are accounting invariants (who is recorded
as responsible, why, and exactly-once execution), not an authorization
mechanism — this function trusts its caller to have already established
that *actor* is entitled to make this correction.

Isolation, by construction, not by policy: this module imports nothing
from wallet_ledger, ib_treasury_settlement, ib_commission,
ib_commission_triggers, or any Treasury/trading-engine module. Creating
a BrokerEconomicAdjustment can only ever write BrokerLedger and
BrokerEconomicAdjustment rows — never a TradingAccount, Wallet, Deposit,
Withdrawal, TreasuryOperationRequest, or IBCommissionObligation.

Not for backfill or historical revenue reconstruction (BROKER-ECONOMICS-02
FASE A §G found that materially riskier and explicitly out of scope —
BrokerLedger.created_at is auto_now_add and interacts with IB sweep
windows in ways a naive backfill could exploit). There is no batch/bulk
entry point here by design; one call is one deliberate, reasoned,
attributed correction.

Never modifies or deletes an existing BrokerLedger row. Every correction
is a new, separate, signed entry — see BrokerEconomicAdjustment's own
docstring in models.py for the full accounting rationale.
"""
import uuid
from decimal import Decimal, InvalidOperation

from django.db import transaction

from .models import BrokerEconomicAdjustment, BrokerLedger, TradingAccount


class InvalidEconomicAdjustment(Exception):
    """Raised for a structurally invalid request (amount==0, missing reason, invalid reference)."""


def _new_adjustment_reference() -> str:
    return f"BEA-{uuid.uuid4().hex[:12].upper()}"


def create_broker_economic_adjustment(
    *, amount, reason: str, actor, idempotency_key: str,
    source_ledger=None, source_account=None, symbol=None,
    reverses=None, meta=None,
):
    """
    Create exactly one compensating BrokerLedger.REV_ADJUSTMENT row and
    exactly one linked BrokerEconomicAdjustment sidecar row, atomically.

    Returns the created BrokerEconomicAdjustment on success, or the
    PRE-EXISTING one if idempotency_key was already used — a repeat call
    is a no-op, never a second BrokerLedger row.

    reverses, when given, must be an existing BrokerEconomicAdjustment
    that is itself not already a reversal (one-hop-only) and has not
    already been reversed by another row — both checked under a row
    lock on the target to close the concurrent-duplicate-reversal race;
    the OneToOneField on BrokerEconomicAdjustment.reverses is the
    database-level backstop behind this check.

    reverses also requires amount == -reverses.amount, exactly — a
    reversal must neutralize the target's economic effect completely,
    never partially, never in excess, never by an unrelated value. This
    is REVERSAL-AMOUNT-INTEGRITY-01 (closed after BROKER-ECONOMICS-02B's
    pre-closure certification found the gap): without it, a row could be
    structurally marked as "reversed" via reverses_id while the combined
    economic effect (original + reversal) was not actually zero — a
    permanent, unfixable-except-by-another-entry misstatement, since
    BrokerEconomicAdjustment rows are never edited or deleted. Checked
    under the same row lock used for the one-hop/already-reversed
    checks, against the authoritative locked value, not a possibly-
    stale amount the caller might be holding.

    A concurrent duplicate request that slips past the idempotency
    pre-check is rejected by the database's UNIQUE constraint on
    idempotency_key, which aborts this entire transaction (including the
    BrokerLedger row just created inside it) — leaving zero partial or
    duplicate economic effect. The caller sees the resulting
    IntegrityError and may simply retry the exact same call: the retry's
    pre-check will then find the winning row and return it cleanly. This
    mirrors simulator.owner_actions's own established idempotency
    pattern exactly (pre-check + DB-UNIQUE backstop, not a catch-and-
    recover branch on the same call).
    """
    try:
        amount = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError):
        raise InvalidEconomicAdjustment("amount must be a valid decimal value.")
    if amount == 0:
        raise InvalidEconomicAdjustment("amount must not be zero.")
    if not reason or not reason.strip():
        raise InvalidEconomicAdjustment("reason is required.")
    if actor is None or not getattr(actor, "pk", None):
        raise InvalidEconomicAdjustment("actor must be a persisted user.")
    if not idempotency_key or not idempotency_key.strip():
        raise InvalidEconomicAdjustment("idempotency_key is required.")
    if source_ledger is not None and not isinstance(source_ledger, BrokerLedger):
        raise InvalidEconomicAdjustment("source_ledger must be a BrokerLedger instance.")
    if source_account is not None and not isinstance(source_account, TradingAccount):
        raise InvalidEconomicAdjustment("source_account must be a TradingAccount instance.")
    if reverses is not None and not isinstance(reverses, BrokerEconomicAdjustment):
        raise InvalidEconomicAdjustment("reverses must be a BrokerEconomicAdjustment instance.")

    with transaction.atomic():
        existing = BrokerEconomicAdjustment.objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return existing

        if source_ledger is not None:
            if not BrokerLedger.objects.filter(pk=source_ledger.pk).exists():
                raise InvalidEconomicAdjustment(
                    f"source_ledger #{source_ledger.pk} does not exist."
                )

        if reverses is not None:
            # Lock the target row for the duration of this transaction —
            # this is what actually closes the concurrent-double-reversal
            # race: a second, simultaneous reversal attempt against the
            # SAME target blocks here until the first commits, then sees
            # the first's committed row on the .filter(...).exists() check
            # below and is rejected cleanly, before ever attempting an
            # insert. .filter().first() (not .get()) so an invalid pk
            # raises our own exception type, not a raw DoesNotExist.
            reverses = (
                BrokerEconomicAdjustment.objects.select_for_update()
                .filter(pk=reverses.pk).first()
            )
            if reverses is None:
                raise InvalidEconomicAdjustment("reverses target does not exist.")
            if reverses.reverses_id is not None:
                raise InvalidEconomicAdjustment(
                    f"adjustment {reverses.reference} is itself a reversal — "
                    "cannot reverse a reversal (one-hop-only)."
                )
            if BrokerEconomicAdjustment.objects.filter(reverses_id=reverses.pk).exists():
                raise InvalidEconomicAdjustment(
                    f"adjustment {reverses.reference} has already been reversed."
                )
            # BROKER-ECONOMICS-02B — REVERSAL-AMOUNT-INTEGRITY-01. A
            # reversal must neutralize EXACTLY the economic effect of the
            # row it reverses — never a partial, over-, or arbitrary
            # amount. Compared against `reverses.amount` as read under
            # the select_for_update() lock above, not a possibly-stale
            # value the caller might be holding. Checked here (not in
            # the pre-atomic validation block) because it depends on the
            # locked, authoritative target row.
            if amount != -reverses.amount:
                raise InvalidEconomicAdjustment(
                    f"reversal amount must be exactly the negative of the "
                    f"adjustment it reverses: {reverses.reference} has "
                    f"amount {reverses.amount}, so the reversal amount "
                    f"must be {-reverses.amount}, not {amount}."
                )

        reference = _new_adjustment_reference()

        # The ONE new BrokerLedger row this call ever creates. This
        # remains the single writer of REV_ADJUSTMENT in the codebase —
        # see module docstring.
        ledger_entry = BrokerLedger.objects.create(
            revenue_type=BrokerLedger.REV_ADJUSTMENT,
            amount=amount,
            source_account=source_account,
            symbol=symbol,
            meta={
                "adjustment_reference": reference,
                "source_ledger_id": source_ledger.pk if source_ledger else None,
                "reverses_reference": reverses.reference if reverses else None,
            },
        )

        adjustment = BrokerEconomicAdjustment.objects.create(
            reference=reference,
            amount=amount,
            reason=reason,
            actor=actor,
            idempotency_key=idempotency_key,
            source_ledger=source_ledger,
            source_account=source_account,
            symbol=symbol,
            reverses=reverses,
            created_ledger_entry=ledger_entry,
            meta=meta or {},
        )

        return adjustment
