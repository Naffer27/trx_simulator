# simulator/payout_providers.py
"""
FIX-02A.2 — NowPayments Adapter.

Thin translation layer over simulator/nowpayments.py (untouched, zero
lines modified). Never reimplements HTTP/auth/timeout/payload logic
that already exists there — only wraps the existing low-level client
and translates its raw exceptions/responses into a normalized,
provider-agnostic contract the orchestrator can reason about.

Error classification depends on WHERE the failure happened, not on the
exception class alone (Design Lock Correction #5) — see
NowPaymentsAdapter.create_payout()'s docstring for exactly how this is
achieved without modifying or duplicating nowpayments.py's internals.

No capability is claimed unless the current nowpayments.py demonstrably
supports it: status_query and cancel are both False — no GET endpoint
for payout status/cancellation exists anywhere in this codebase.
"""
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

import requests
from django.utils import timezone

from . import nowpayments as _np
from .models import PayoutAttempt

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Normalized errors — no requests.* exception ever crosses this
# module's boundary unwrapped.
# ─────────────────────────────────────────────

class ProviderError(Exception):
    """Base for all normalized provider errors."""


class ProviderAuthError(ProviderError):
    """Failed obtaining the JWT (/v1/auth) — the payout POST
    (/v1/payout) was NEVER attempted. Pre-send-safe by construction."""


class ProviderTimeoutError(ProviderError):
    """No response received for the /v1/payout POST itself (timeout or
    connection error) — ambiguous: the payout may or may not have been
    received on NowPayments' side."""


class ProviderUnavailableError(ProviderError):
    """A response WAS received for the /v1/payout POST but it was a
    non-2xx status (4xx or 5xx) — ambiguous. This codebase does not
    demonstrate what a 4xx from this specific endpoint means on
    NowPayments' side, so it is NOT treated as a safe pre-send
    rejection (Design Lock — narrow ProviderAuthError-only safe case)."""


class ProviderResponseError(ProviderError):
    """2xx received but the body couldn't be parsed / was missing the
    expected withdrawals[]/id fields — ambiguous, and arguably the most
    dangerous case to mishandle (the provider said OK and we couldn't
    read the details)."""


# ─────────────────────────────────────────────
# Normalized shapes
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class PayoutSubmissionResult:
    accepted: bool
    provider_reference: str
    provider_batch_id: str
    provider_amount: Decimal | None
    raw_status: str


@dataclass(frozen=True)
class ProviderPayoutEvent:
    provider: str
    provider_reference: str
    provider_batch_id: str
    normalized_status: str   # a PayoutAttempt.STATUS_* value
    raw_status: str
    provider_amount: Decimal | None
    occurred_at: datetime
    # FIX-02A.4 — this individual event's own raw sub-payload (e.g. one
    # withdrawals[] entry), NOT the whole batch body. Used only to
    # compute a per-event fingerprint that can't collide with a sibling
    # event from the same delivery — see compute_webhook_event_fingerprint().
    # Defaulted so existing call sites (tests constructing this dataclass
    # directly) keep working unmodified.
    raw_event_payload: dict = field(default_factory=dict)


# ─────────────────────────────────────────────
# FIX-02A.4 — provider-agnostic active-reconciliation contract.
# The reconciliation service (payout_orchestrator.py) NEVER sees a
# provider-specific status string — only these normalized outcomes.
# ─────────────────────────────────────────────

class PayoutLookupOutcome(str, Enum):
    FOUND_PROCESSING = "found_processing"
    FOUND_COMPLETED  = "found_completed"
    FOUND_FAILED     = "found_failed"
    NOT_FOUND        = "not_found"
    UNAVAILABLE      = "unavailable"   # provider down/timeout/transient error
    AMBIGUOUS        = "ambiguous"     # response present but not confidently mappable
    UNSUPPORTED      = "unsupported"   # this adapter has no lookup capability at all


@dataclass(frozen=True)
class PayoutLookupResult:
    outcome: PayoutLookupOutcome
    provider_reference: str = ""
    provider_batch_id: str = ""
    raw_provider_status: str = ""
    raw_metadata: dict = field(default_factory=dict)


# Mirrors the _NP_TO_STATUS mapping already used in views.py today,
# retargeted at PayoutAttempt statuses instead of WithdrawalRequest
# statuses. Unrecognized raw statuses are discarded by the caller
# (parse_webhook returns no event for them) — same as today's `continue`.
_RAW_STATUS_TO_NORMALIZED = {
    "FINISHED": PayoutAttempt.STATUS_COMPLETED,
    "FAILED":   PayoutAttempt.STATUS_FAILED,
    "ROLLING":  PayoutAttempt.STATUS_PROCESSING,
    "CREATED":  PayoutAttempt.STATUS_PROCESSING,
}


class NowPaymentsAdapter:
    provider_name = "nowpayments"
    # FIX-02A.4 — explicit, desambiguated capabilities (replaces the old
    # ambiguous status_query/cancel pair). Every flag here is backed by
    # a demonstrated absence in nowpayments.py: no GET /v1/payout*
    # endpoint of any kind exists in this codebase, and
    # create_payout_with_token()'s payload never includes any
    # id/order_id/reference field the provider could later recognize.
    capabilities = {
        "supports_external_idempotency":          False,
        "supports_lookup_by_provider_reference":   False,
        "supports_lookup_by_provider_request_id":  False,
        "supports_lookup_by_batch":                False,
        "supports_webhooks":                       True,
    }

    def estimate(self, amount_usd, asset) -> Decimal:
        """
        GET /v1/estimate (via nowpayments.estimate_price(), unmodified).
        Side-effect-free — creates nothing, moves nothing. Any failure
        here means structurally nothing was ever submitted; the caller
        (payout_orchestrator) treats this as "nothing happened", never
        as a PayoutAttempt-level outcome.
        """
        try:
            return _np.estimate_price(amount_usd, asset)
        except Exception as exc:
            raise ProviderUnavailableError(f"estimate_price failed: {exc}") from exc

    def create_payout(self, attempt, *, callback_url: str = "") -> PayoutSubmissionResult:
        """
        Classifies failures by WHICH call raised them, not by exception
        class alone (Design Lock Correction #5) — the same requests
        exception classes can come from either the JWT call or the
        payout POST, so exception-class-alone cannot tell them apart.

        Exactly ONE real call to _get_jwt_token() happens on this path
        (FIX-02A.2 JWT-blocker fix — nowpayments.create_payout_with_token()
        takes the token as a parameter and never fetches its own, so
        there is no second, redundant auth round-trip to misclassify).
        If auth fails, the /v1/payout POST is structurally impossible to
        have been attempted — ProviderAuthError, pre-send-safe. Only a
        failure from create_payout_with_token() itself — which begins
        with the real POST, nothing else — is ambiguous.
        """
        try:
            token = _np._get_jwt_token()
        except Exception as exc:
            raise ProviderAuthError(
                f"NowPayments auth failed — /v1/payout was never attempted: {exc}"
            ) from exc

        try:
            data = _np.create_payout_with_token(
                attempt.destination_address,
                attempt.requested_asset,
                attempt.provider_amount,
                attempt.withdrawal_request_id,
                callback_url,
                token,
            )
        except requests.exceptions.Timeout as exc:
            raise ProviderTimeoutError(f"payout POST timed out: {exc}") from exc
        except requests.exceptions.ConnectionError as exc:
            raise ProviderTimeoutError(f"payout POST connection error: {exc}") from exc
        except requests.exceptions.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            raise ProviderUnavailableError(f"payout POST returned HTTP {status}: {exc}") from exc
        except Exception as exc:
            raise ProviderResponseError(f"payout POST failed unexpectedly: {exc}") from exc

        try:
            batch_wds = data.get("withdrawals", [])
            provider_reference = str(batch_wds[0].get("id", "")) if batch_wds else ""
            provider_batch_id = str(data.get("id", ""))
            raw_status = str(data.get("status", ""))
        except (AttributeError, TypeError, KeyError, IndexError) as exc:
            raise ProviderResponseError(f"payout POST returned an unparseable body: {exc}") from exc

        return PayoutSubmissionResult(
            accepted=True,
            provider_reference=provider_reference,
            provider_batch_id=provider_batch_id,
            provider_amount=attempt.provider_amount,
            raw_status=raw_status,
        )

    def lookup_payout(self, attempt) -> PayoutLookupResult:
        """
        FIX-02A.4 — active reconciliation lookup. NowPayments, as
        integrated in this codebase, has no GET endpoint for payout
        status by any key (confirmed: no such function exists in
        nowpayments.py, capabilities above are all False) — this is a
        real, verified limitation of THIS adapter, not a limitation of
        the reconciliation core. Always returns UNSUPPORTED, never
        attempts any HTTP call. Do NOT invent an endpoint here — if
        NowPayments ever adds one, implement it against real,
        documented behavior, not a guess.
        """
        return PayoutLookupResult(outcome=PayoutLookupOutcome.UNSUPPORTED)

    def parse_webhook(self, raw_body: bytes, headers) -> list[ProviderPayoutEvent] | None:
        """
        Verifies the HMAC signature (nowpayments.verify_ipn_signature(),
        unmodified) then parses the batch payload into normalized
        events — one per withdrawals[] entry, same shape the current
        withdraw_payout_callback already iterates. Returns None on
        invalid signature or unparseable JSON (caller returns 400,
        identical to today's behavior). raw_status never crosses into
        PayoutAttempt.status directly — only normalized_status does.
        """
        sig = headers.get("x-nowpayments-sig", "")
        if not _np.verify_ipn_signature(raw_body, sig):
            return None

        try:
            data = json.loads(raw_body)
        except (json.JSONDecodeError, TypeError):
            return None

        batch_id = str(data.get("id", ""))
        occurred_at = timezone.now()
        events: list[ProviderPayoutEvent] = []
        for wd in data.get("withdrawals", []):
            raw_status = str(wd.get("status", "")).upper()
            normalized = _RAW_STATUS_TO_NORMALIZED.get(raw_status)
            if not normalized:
                continue
            events.append(ProviderPayoutEvent(
                provider=self.provider_name,
                provider_reference=str(wd.get("id", "")),
                provider_batch_id=batch_id,
                normalized_status=normalized,
                raw_status=raw_status,
                provider_amount=None,
                occurred_at=occurred_at,
                raw_event_payload=wd,
            ))
        return events


# ─────────────────────────────────────────────
# FIX-02A.4 — durable inbox fingerprint. Provider-aware, deterministic,
# never based on a local timestamp — must survive redelivery, restarts,
# multiple workers.
# ─────────────────────────────────────────────

def _canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def compute_webhook_event_fingerprint(event: ProviderPayoutEvent) -> str:
    """
    Computed from the individual event's own identifying fields PLUS
    its own raw sub-payload (event.raw_event_payload) — not the whole
    batch body. A single webhook delivery can carry multiple
    ProviderPayoutEvent entries (NowPayments: one per withdrawals[]
    item); each gets its own fingerprint because each entry's own
    raw_event_payload differs (at minimum by that entry's own `id`).
    Redelivery of the exact same event reproduces the exact same
    fingerprint (same inputs), which is what makes dedup on retry work.
    """
    fingerprint_input = "|".join([
        event.provider,
        event.provider_batch_id,
        event.provider_reference,
        event.raw_status,
        _canonical_json(event.raw_event_payload),
    ])
    return hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────
# FIX-02A.4 — provider registry. Lives here, not in payout_orchestrator.py,
# so the orchestrator/reconciliation core never imports a concrete
# adapter class by name — only get_adapter_for_provider(). No circular
# import risk: this module already imports nothing from
# payout_orchestrator.py.
# ─────────────────────────────────────────────

_PROVIDER_REGISTRY = {
    "nowpayments": NowPaymentsAdapter,
}


def get_adapter_for_provider(provider_name: str):
    try:
        return _PROVIDER_REGISTRY[provider_name]()
    except KeyError:
        raise ValueError(f"No adapter registered for provider={provider_name!r}")
