# simulator/payment_webhook_inbox.py
"""
simulator/payment_webhook_inbox.py — BROKER-ECONOMICS-04C.3 (FASE B).

CORE SERVICE for PaymentWebhookEvent. The one and only sanctioned entry
point that may create a durable payment-webhook evidence row.

Evidence capture only. Zero economic interpretation. This module never
reads, inspects, or reasons about a `fee` key, `outcome_amount`, or any
other economically-meaningful field inside the payload it captures — it
persists the parsed dict exactly as received and returns.

Call ONLY from views.py::deposit_callback, after signature verification
and JSON parsing have both already succeeded, and BEFORE the Deposit
lookup — so even an unrecognized/orphan payment_id is preserved. Placed
before the view's own transaction.atomic() block that credits the
Wallet / activates a challenge, so a later rollback in that block can
never erase evidence already durably captured here.

Fingerprint discipline: reuses the exact proven recipe already certified
for PayoutWebhookEvent.event_fingerprint
(payout_providers.py::compute_webhook_event_fingerprint /
_canonical_json — sha256 over pipe-joined identifying fields plus a
sort_keys/compact-separator canonical JSON encoding of the payload) —
re-implemented locally, not imported, so this module never depends on
payout_orchestrator.py/payout_providers.py. Same algorithm, zero
coupling between the two rails, per instruction.

Idempotency: DB-enforced via PaymentWebhookEvent.event_fingerprint's own
UniqueConstraint (unique=True) — race-safe create-inside-a-savepoint,
IntegrityError -> fetch-the-winner, the same pattern
get_or_create_webhook_event() already uses for payouts. Never uses
exists()/get_or_create() as the primary guard for the same reason that
function doesn't: only a DB-level unique constraint is authoritative
under a true concurrent race.

Fail-open, by design: capture_payment_webhook_event() never raises.
A failure here (e.g. a transient DB error) must never block, alter, or
delay the real economic processing (Wallet credit, challenge
activation) that follows it in deposit_callback — mirrors AUDIT-02's
own "never allowed to affect the payout" contract, applied to evidence
capture instead of an audit log line.

Isolation, by construction: this module imports nothing from
wallet_ledger, challenge_revenue, withdrawal_economics,
funded_economics, ib_commission, payout_orchestrator,
payout_providers, or any Treasury/trading-engine module. It can only
ever write a single PaymentWebhookEvent row.
"""
import hashlib
import json
import logging

from .models import PaymentWebhookEvent

logger = logging.getLogger(__name__)


def _canonical_json(obj) -> str:
    """Same recipe as payout_providers.py::_canonical_json — deterministic,
    sorted-key, compact-separator JSON encoding, re-implemented locally."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _compute_fingerprint(provider: str, payment_id: str, order_id: str,
                          payment_status: str, raw_payload: dict) -> str:
    """Same recipe as payout_providers.py::compute_webhook_event_fingerprint
    — sha256 over pipe-joined identifying fields plus the canonical
    payload — re-implemented locally rather than imported (no dependency
    on payout_orchestrator.py/payout_providers.py)."""
    fingerprint_input = "|".join([
        provider, payment_id, order_id, payment_status,
        _canonical_json(raw_payload),
    ])
    return hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()


def capture_payment_webhook_event(
    raw_payload: dict, *, provider: str = "nowpayments",
) -> "PaymentWebhookEvent | None":
    """
    Durably capture one already-signature-verified, already-parsed
    payment webhook payload. Returns the PaymentWebhookEvent row (new or
    the pre-existing one on an exact replay), or None if capture itself
    failed — NEVER raises.

    raw_payload must already be the parsed dict (json.loads(body)) — this
    function does no parsing or verification of its own; both are the
    caller's responsibility and must happen first.
    """
    try:
        payment_id = str(raw_payload.get("payment_id", "") or "")
        order_id = str(raw_payload.get("order_id", "") or "")
        payment_status = str(raw_payload.get("payment_status", "") or "")

        fingerprint = _compute_fingerprint(
            provider, payment_id, order_id, payment_status, raw_payload,
        )

        try:
            from django.db import transaction
            with transaction.atomic():
                return PaymentWebhookEvent.objects.create(
                    provider=provider,
                    event_fingerprint=fingerprint,
                    payment_id=payment_id,
                    order_id=order_id,
                    payment_status=payment_status,
                    raw_payload=raw_payload,
                )
        except Exception:
            # Race-safe fetch — an identical delivery (or a genuine
            # concurrent duplicate) already won; this is a no-op, not an
            # error. Any OTHER failure also falls here as a defensive
            # fallback lookup, then re-raises to the outer fail-open
            # handler below if that fetch also finds nothing.
            existing = PaymentWebhookEvent.objects.filter(
                event_fingerprint=fingerprint,
            ).first()
            if existing is not None:
                return existing
            raise
    except Exception as exc:
        # Fail-open — evidence capture must never block or alter the
        # real deposit/challenge processing that follows it.
        logger.warning("[payment_webhook_inbox] capture failed: %s", exc)
        return None
