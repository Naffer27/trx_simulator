# simulator/provider_cost_adapters.py
"""
simulator/provider_cost_adapters.py — BROKER-ECONOMICS-04C.4.

Adapter boundary between provider-specific evidence (PaymentWebhookEvent /
PayoutWebhookEvent) and the provider-agnostic ProviderCostCandidate a
provider_cost_inbox.py can persist.

── Contract ───────────────────────────────────────────────────────────
An adapter is a plain, duck-typed object (no ABC base — mirrors
payout_providers.py::NowPaymentsAdapter's own style) exposing:

    provider_name: str
    def normalize_payment_cost(self, event: PaymentWebhookEvent) -> list[ProviderCostCandidate]
    def normalize_payout_cost(self, event: PayoutWebhookEvent) -> list[ProviderCostCandidate]

An adapter MAY interpret provider-specific fields inside raw_payload,
decide quality/finality from provider-specific status semantics, and map
provider-specific currency/category codes.

An adapter MUST NOT touch Wallet, Treasury, or trading; MUST NOT compute
client fees or change challenge/withdrawal revenue; MUST NOT write
BrokerLedger directly — only provider_cost_economics.py may ever do that.

Registered through get_cost_adapter_for_provider() below — a small,
NEW registry, deliberately SEPARATE from payout_providers.py's existing
payout-submission adapter registry, so the cost-normalization rail and
the payout-submission rail stay uncoupled (same "reuse the discipline,
not the code, keep the rails separate" instruction 04C.3 established). A
future Treasury Engine adapter is a thin shim living inside simulator/
that implements this same duck-typed contract — this module never
imports from treasury_engine, and treasury_engine is never touched by
this block.

── NowPayments reality (04C.1/04C.2/04C.4) ──────────────────────────────
Evidence-supported normalization only. If PaymentWebhookEvent.raw_payload
or PayoutWebhookEvent.raw_payload contains no `fee` object (the confirmed
empirical reality for 4/4 real historical payments checked in 04C.2),
zero candidates are produced — never a fabricated ACTUAL zero, never an
UNKNOWN placeholder row. Never computes actually_paid - outcome_amount as
cost. Never hardcodes NowPayments' public pricing percentages as actual
transaction cost. usd_value is populated ONLY when fee.currency == "usd"
literally — never inferred, never fetched from a market rate.
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .models import PaymentWebhookEvent, PayoutWebhookEvent, ProviderCostRecord

# Terminal payment_status values (PaymentWebhookEvent, lowercase, as
# NowPayments sends them) beyond which a reported fee is not expected to
# change further. Deliberately a LOCAL, adapter-owned set — broader than
# Deposit.CREDITED_STATUSES (which governs Wallet crediting, a different
# question) since a failed/expired/refunded payment's fee, if reported,
# is equally final.
_NP_TERMINAL_PAYMENT_STATUSES = frozenset({
    "finished", "confirmed", "failed", "expired", "refunded",
})

# Terminal raw_status values (PayoutWebhookEvent.raw_status, uppercase,
# as normalized by payout_providers.py::NowPaymentsAdapter.parse_webhook).
_NP_TERMINAL_PAYOUT_STATUSES = frozenset({"FINISHED", "FAILED"})

# (raw fee sub-field, cost_type) pairs this adapter recognizes inside a
# NowPayments `fee` object — see 04C.1's documentation research.
_NP_FEE_FIELD_MAP = (
    ("serviceFee",    ProviderCostRecord.COST_PROVIDER_SERVICE),
    ("depositFee",    ProviderCostRecord.COST_NETWORK),
    ("withdrawalFee", ProviderCostRecord.COST_NETWORK),
)


@dataclass(frozen=True)
class ProviderCostCandidate:
    """
    Plain, unsaved normalization result — NOT yet a persisted
    ProviderCostRecord. provider_cost_inbox.py::capture_provider_cost_candidate()
    is the only thing that turns one of these into a durable row.
    """
    provider: str
    operation_type: str
    cost_type: str
    provider_reference: str
    amount: Decimal
    currency: str
    usd_value: "Decimal | None"
    quality: str
    is_final: bool
    occurred_at: datetime
    source_payment_webhook_event: "PaymentWebhookEvent | None" = None
    source_payout_webhook_event: "PayoutWebhookEvent | None" = None
    corrects: "ProviderCostRecord | None" = None
    meta: dict = field(default_factory=dict)


def _parse_decimal(raw_value) -> "Decimal | None":
    if raw_value is None:
        return None
    try:
        return Decimal(str(raw_value))
    except (InvalidOperation, TypeError, ValueError):
        return None


class NowPaymentsCostAdapter:
    """One adapter for one provider. Interprets NowPayments-specific
    evidence only — every other module in this design reads only the
    provider-agnostic ProviderCostCandidate/ProviderCostRecord it produces."""

    provider_name = "nowpayments"

    def _normalize_fee_object(
        self, fee: dict, *, operation_type: str, provider_reference: str,
        is_final: bool, occurred_at: datetime,
        source_payment_webhook_event=None, source_payout_webhook_event=None,
    ) -> list[ProviderCostCandidate]:
        if not isinstance(fee, dict):
            return []

        currency = str(fee.get("currency") or "").strip().lower()
        candidates: list[ProviderCostCandidate] = []

        for raw_field, cost_type in _NP_FEE_FIELD_MAP:
            raw_value = fee.get(raw_field)
            if raw_value is None:
                continue
            amount = _parse_decimal(raw_value)
            if amount is None:
                continue
            # 04C.2/04C.4 — usd_value ONLY when the fee's own currency is
            # literally "usd". Never inferred, never fetched from a market
            # rate, never derived from outcome_amount/outcome_currency.
            usd_value = amount if currency == "usd" else None

            candidates.append(ProviderCostCandidate(
                provider=self.provider_name,
                operation_type=operation_type,
                cost_type=cost_type,
                provider_reference=provider_reference,
                amount=amount,
                currency=currency,
                usd_value=usd_value,
                quality=ProviderCostRecord.QUALITY_ACTUAL,
                is_final=is_final,
                occurred_at=occurred_at,
                source_payment_webhook_event=source_payment_webhook_event,
                source_payout_webhook_event=source_payout_webhook_event,
                meta={"raw_fee_field": raw_field, "raw_fee_currency": currency},
            ))
        return candidates

    def normalize_payment_cost(self, event: PaymentWebhookEvent) -> list[ProviderCostCandidate]:
        """
        Deposit-leg cost normalization. Returns zero candidates when
        raw_payload carries no `fee` object — the confirmed empirical
        reality for every real payment checked in 04C.2. Never
        manufactures an ACTUAL zero or an UNKNOWN row to represent that
        silence.
        """
        payload = event.raw_payload or {}
        fee = payload.get("fee")
        if not isinstance(fee, dict):
            return []

        is_final = str(event.payment_status or "").strip().lower() in _NP_TERMINAL_PAYMENT_STATUSES

        return self._normalize_fee_object(
            fee,
            operation_type=ProviderCostRecord.OP_DEPOSIT,
            provider_reference=event.payment_id,
            is_final=is_final,
            occurred_at=event.received_at,
            source_payment_webhook_event=event,
        )

    def normalize_payout_cost(self, event: PayoutWebhookEvent) -> list[ProviderCostCandidate]:
        """
        Withdrawal-leg cost normalization (covers both WithdrawalRequest-
        driven and FUNDED_INTERNAL-driven payouts — both are outbound
        capital-flow legs; operation_type reflects direction, not which
        triggering record produced the payout). Same "zero candidates on
        silence" discipline as normalize_payment_cost().
        """
        payload = event.raw_payload or {}
        fee = payload.get("fee")
        if not isinstance(fee, dict):
            return []

        is_final = str(event.raw_status or "").strip().upper() in _NP_TERMINAL_PAYOUT_STATUSES

        return self._normalize_fee_object(
            fee,
            operation_type=ProviderCostRecord.OP_WITHDRAWAL,
            provider_reference=event.provider_reference,
            is_final=is_final,
            occurred_at=event.received_at,
            source_payout_webhook_event=event,
        )


_COST_ADAPTERS = {
    "nowpayments": NowPaymentsCostAdapter,
}


def get_cost_adapter_for_provider(provider_name: str):
    """
    Returns an adapter instance for *provider_name*, or None if no cost
    adapter is registered for it — callers must treat None as "zero
    candidates," never as an error.
    """
    adapter_cls = _COST_ADAPTERS.get(provider_name)
    if adapter_cls is None:
        return None
    return adapter_cls()
