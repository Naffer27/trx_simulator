# simulator/provider_cost_inbox.py
"""
simulator/provider_cost_inbox.py — BROKER-ECONOMICS-04C.4.

CORE SERVICE for ProviderCostRecord. The one and only sanctioned entry
point that may create a normalized provider-cost evidence row.

Takes a ProviderCostCandidate (provider_cost_adapters.py) — a plain,
unsaved dataclass an adapter produced by interpreting PaymentWebhookEvent/
PayoutWebhookEvent evidence — and turns it into a persisted, fingerprinted,
idempotent ProviderCostRecord. This module does no interpretation of its
own: it trusts the candidate's fields exactly as given, the same relationship
payment_webhook_inbox.py::capture_payment_webhook_event() has to its caller.

Fingerprint discipline: content-based, deterministic, re-implemented
locally (not imported from payout_providers.py/payment_webhook_inbox.py) —
same recipe family (sha256 over pipe-joined identifying fields plus a
sort_keys/compact-separator canonical JSON encoding), same "reuse the
discipline, not the code" instruction already established in 04C.3.
Fingerprinted over (provider, operation_type, cost_type,
provider_reference, canonical_json(evidence_subset)) where
evidence_subset is exactly the candidate's own defining values (amount,
currency, usd_value, quality, is_final, occurred_at) — an identical
candidate always reproduces the same fingerprint (dedup on replay/retry/
polling+webhook overlap); a genuine value/quality/finality transition, or
a correction's new evidence, always produces a different one (a new row,
never conflated with the one it supersedes).

Idempotency: DB-enforced via ProviderCostRecord.cost_fingerprint's own
UniqueConstraint (unique=True) — race-safe create-inside-a-savepoint,
IntegrityError -> fetch-the-winner, the same pattern every other durable
inbox in this codebase already uses. Never exists()/get_or_create() as
the primary guard — only a DB-level unique constraint is authoritative
under a true concurrent race.

Fail-open, by design: capture_provider_cost_candidate() never raises. A
failure here must never block whatever webhook-processing flow called it
(deposit_callback / withdraw_payout_callback) — mirrors
payment_webhook_inbox.py's own fail-open contract exactly.

Isolation, by construction: this module imports nothing from
wallet_ledger, challenge_revenue, withdrawal_economics, funded_economics,
ib_commission, payout_orchestrator, payout_providers, or any Treasury/
trading-engine module. It can only ever write a single ProviderCostRecord
row — never a BrokerLedger row (that is provider_cost_economics.py's sole
responsibility).
"""
import hashlib
import json
import logging

from .models import ProviderCostRecord

logger = logging.getLogger(__name__)


def _canonical_json(obj) -> str:
    """Same recipe family as payout_providers.py::_canonical_json /
    payment_webhook_inbox.py::_canonical_json — deterministic, sorted-key,
    compact-separator JSON encoding, re-implemented locally."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _compute_fingerprint(candidate) -> str:
    """Content-based identity — never based on a local timestamp or
    received_at, so a genuine replay (identical candidate, delivered
    again) always reproduces the same fingerprint."""
    evidence_subset = {
        "amount": str(candidate.amount),
        "currency": candidate.currency,
        "usd_value": str(candidate.usd_value) if candidate.usd_value is not None else None,
        "quality": candidate.quality,
        "is_final": candidate.is_final,
        "occurred_at": candidate.occurred_at.isoformat(),
        "corrects_id": candidate.corrects.pk if candidate.corrects is not None else None,
    }
    fingerprint_input = "|".join([
        candidate.provider, candidate.operation_type, candidate.cost_type,
        candidate.provider_reference, _canonical_json(evidence_subset),
    ])
    return hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()


def capture_provider_cost_candidate(candidate) -> "ProviderCostRecord | None":
    """
    Durably capture one normalized ProviderCostCandidate. Returns the
    created ProviderCostRecord (new, or the pre-existing one on an exact
    replay), or None if capture itself failed — NEVER raises.
    """
    try:
        fingerprint = _compute_fingerprint(candidate)

        try:
            from django.db import transaction
            with transaction.atomic():
                return ProviderCostRecord.objects.create(
                    provider=candidate.provider,
                    operation_type=candidate.operation_type,
                    cost_type=candidate.cost_type,
                    provider_reference=candidate.provider_reference,
                    source_payment_webhook_event=candidate.source_payment_webhook_event,
                    source_payout_webhook_event=candidate.source_payout_webhook_event,
                    amount=candidate.amount,
                    currency=candidate.currency,
                    usd_value=candidate.usd_value,
                    quality=candidate.quality,
                    is_final=candidate.is_final,
                    occurred_at=candidate.occurred_at,
                    cost_fingerprint=fingerprint,
                    corrects=candidate.corrects,
                    meta=candidate.meta,
                )
        except Exception:
            # Race-safe fetch — an identical delivery (or a genuine
            # concurrent duplicate) already won; this is a no-op, not an
            # error. Any OTHER failure also falls here as a defensive
            # fallback lookup, then re-raises to the outer fail-open
            # handler below if that fetch also finds nothing.
            existing = ProviderCostRecord.objects.filter(
                cost_fingerprint=fingerprint,
            ).first()
            if existing is not None:
                return existing
            raise
    except Exception as exc:
        # Fail-open — cost-evidence capture must never block or alter
        # whatever webhook-processing flow called it.
        logger.warning("[provider_cost_inbox] capture failed: %s", exc)
        return None
