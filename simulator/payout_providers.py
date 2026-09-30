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
    # A PayoutAttempt.STATUS_* value, or None when raw_status isn't in
    # this adapter's recognized vocabulary (WITHDRAWAL-E2E-02B) — an
    # honest "we don't know" signal, never guessed. Callers must persist
    # the event regardless and must never attempt a financial transition
    # when this is None (see payout_orchestrator.py's two dispatchers).
    normalized_status: str | None
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
# statuses. WITHDRAWAL-E2E-02B — a raw status NOT in this map no longer
# means "discard the event" (see parse_webhook()): it means "the event
# is persisted with normalized_status=None and routed to MANUAL_REVIEW
# without any attempted financial transition" — the .get() below then
# legitimately returns None for that case, consumed explicitly.
# WITHDRAWAL-E2E-02C — deliberately NOT extended with the official
# in-flight statuses (new/creating/waiting/processing/sending) despite
# the fuller vocabulary confirmed in the 02C design report. Reason: this
# map feeds _apply_attempt_webhook() -> transition_payout_attempt(),
# which is called for an attempt in WHATEVER status it currently holds —
# by the time any webhook can arrive, _apply_submission_success() has
# already synchronously moved the attempt to PROCESSING, and
# ALLOWED_TRANSITIONS[PROCESSING] = {COMPLETED, FAILED} has no
# PROCESSING -> PROCESSING self-loop. A webhook redelivering an
# in-flight status while already PROCESSING would raise
# InvalidPayoutAttemptTransition, uncaught, all the way up to the view —
# a real crash risk, not hypothetical (see the payout_orchestrator.py
# comment above process_webhook_event()'s webhook-status guard). Also
# deliberately NOT extended with "rejected_not_checked" or
# "cancelled"/"canceled": the 02C design report flagged genuine
# ambiguity about whether funds could have moved before either fires,
# and this map's FAILED entries drive an AUTOMATIC refund via
# _apply_confirmed_failure_with_refund() — mapping an ambiguous status
# here risks exactly the double-payment the whole refund engine exists
# to prevent. All of these safely fall through to normalized_status=None
# (WITHDRAWAL-E2E-02B) -> MANUAL_REVIEW, zero mutation attempted, same
# as any other genuinely unrecognized status.
_RAW_STATUS_TO_NORMALIZED = {
    "FINISHED": PayoutAttempt.STATUS_COMPLETED,
    "FAILED":   PayoutAttempt.STATUS_FAILED,
    # WITHDRAWAL-E2E-02B — confirmed via direct GET /v1/payout/{id}
    # (WITHDRAWAL-E2E-02A forensic audit, WR17): NowPayments' own
    # terminal rejection status for a payout that failed provider-side
    # verification. No blockchain transaction exists when this fires
    # (hash: null) — same terminal-failure semantics as "FAILED".
    "REJECTED": PayoutAttempt.STATUS_FAILED,
    "ROLLING":  PayoutAttempt.STATUS_PROCESSING,
    "CREATED":  PayoutAttempt.STATUS_PROCESSING,
}

# WITHDRAWAL-E2E-02B/02C — separate vocabulary for the active GET
# /v1/payout/{id} lookup (lookup_payout() below). Deliberately NOT the
# same dict as _RAW_STATUS_TO_NORMALIZED — the two vocabularies overlap
# but answer different questions, and critically differ in SAFETY here:
# reconcile_unknown_payout_attempts() only ever calls lookup_payout()
# for an attempt currently in STATUS_UNKNOWN (see that function's own
# query filter), and UNKNOWN -> PROCESSING IS an allowed transition — so
# the same-status-crash risk that keeps new/creating/waiting/processing/
# sending OUT of _RAW_STATUS_TO_NORMALIZED above does NOT apply here,
# and WITHDRAWAL-E2E-02C adds them. "rejected_not_checked" and
# "cancelled"/"canceled" remain deliberately unmapped for the same
# fund-movement-ambiguity reason as above — they fall through to the
# dict's own .get(raw_status, AMBIGUOUS) default, which
# reconcile_unknown_payout_attempts() already treats as "leave UNKNOWN,
# mutate nothing" (Design Lock point 8/9, unchanged).
_RAW_STATUS_TO_LOOKUP_OUTCOME = {
    "FINISHED":   PayoutLookupOutcome.FOUND_COMPLETED,
    "FAILED":     PayoutLookupOutcome.FOUND_FAILED,
    "REJECTED":   PayoutLookupOutcome.FOUND_FAILED,
    "CREATING":   PayoutLookupOutcome.FOUND_PROCESSING,
    "SENDING":    PayoutLookupOutcome.FOUND_PROCESSING,
    "ROLLING":    PayoutLookupOutcome.FOUND_PROCESSING,
    "CREATED":    PayoutLookupOutcome.FOUND_PROCESSING,
    # WITHDRAWAL-E2E-02C — official statuses confirmed in the design
    # report, safe to add here specifically (see module comment above).
    "NEW":        PayoutLookupOutcome.FOUND_PROCESSING,
    "WAITING":    PayoutLookupOutcome.FOUND_PROCESSING,
    "PROCESSING": PayoutLookupOutcome.FOUND_PROCESSING,
}


class NowPaymentsAdapter:
    provider_name = "nowpayments"
    # FIX-02A.4 — explicit, desambiguated capabilities.
    # WITHDRAWAL-E2E-02B — supports_lookup_by_provider_reference is now
    # True: GET /v1/payout/{id} is a real, working NowPayments endpoint
    # (confirmed empirically in WITHDRAWAL-E2E-02A against payout_id
    # 5007958386 — HTTP 200, real status returned), implemented below in
    # lookup_payout(). create_payout_with_token()'s payload still never
    # includes an id/order_id/reference field the provider could later
    # recognize, so external idempotency and lookup-by-request-id/-batch
    # remain unsupported.
    capabilities = {
        "supports_external_idempotency":          False,
        "supports_lookup_by_provider_reference":   True,
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
            # WITHDRAWAL-E2E-02B Bug B fix — the individual withdrawal's
            # status lives at withdrawals[0]["status"], never at the top
            # level of the response body (confirmed against WR17's real
            # body in WITHDRAWAL-E2E-02A: the outer object has no "status"
            # key at all). data.get("status", "") always returned "" here,
            # silently losing real, available data (e.g. "CREATING").
            raw_status = str(batch_wds[0].get("status", "")) if batch_wds else ""
        except (AttributeError, TypeError, KeyError, IndexError) as exc:
            raise ProviderResponseError(f"payout POST returned an unparseable body: {exc}") from exc

        return PayoutSubmissionResult(
            accepted=True,
            provider_reference=provider_reference,
            provider_batch_id=provider_batch_id,
            provider_amount=attempt.provider_amount,
            raw_status=raw_status,
        )

    def verify_payout(self, attempt, code: str) -> None:
        """
        WITHDRAWAL-E2E-02C — POST /v1/payout/{batch_id}/verify. Confirms
        NowPayments' own provider-side 2FA code for a payout batch.

        Uses attempt.provider_batch_id (the BATCH id NowPayments assigned
        at creation) — NEVER attempt.provider_reference (the individual
        payout id). Confirmed against the official NowPaymentsIO Node.js
        SDK source (github.com/NowPaymentsIO/nowpayments-sdk-nodejs,
        src/client.js: verifyPayout(batchId, code)) — see the
        WITHDRAWAL-E2E-02C design report for the full contract.

        Same failure classification discipline as create_payout() (Design
        Lock Correction #5) — auth failure is pre-send-safe (the verify
        POST was structurally never attempted); everything past that is
        ambiguous, same as create_payout()'s own POST.

        Returns None on success (no exception raised is the success
        signal — the caller, payout_orchestrator.submit_payout_
        verification(), sets PayoutAttempt.verified_at itself). Raises a
        normalized ProviderError subclass on any failure.

        CRITICAL: `code` is passed straight through to nowpayments.py's
        verify_payout_with_token() and never appears in any exception
        message, log line, or return value constructed in this method or
        in nowpayments.py's own logging on this path.
        """
        try:
            token = _np._get_jwt_token()
        except Exception as exc:
            raise ProviderAuthError(
                f"NowPayments auth failed — payout verify was never attempted: {exc}"
            ) from exc

        try:
            _np.verify_payout_with_token(attempt.provider_batch_id, code, token)
        except requests.exceptions.Timeout as exc:
            raise ProviderTimeoutError(f"payout verify POST timed out: {exc}") from exc
        except requests.exceptions.ConnectionError as exc:
            raise ProviderTimeoutError(f"payout verify POST connection error: {exc}") from exc
        except requests.exceptions.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            raise ProviderUnavailableError(f"payout verify POST returned HTTP {status}: {exc}") from exc
        except Exception as exc:
            raise ProviderResponseError(f"payout verify POST failed unexpectedly: {exc}") from exc

    def lookup_payout(self, attempt) -> PayoutLookupResult:
        """
        WITHDRAWAL-E2E-02B — FIX-02A.4's active reconciliation lookup,
        now genuinely implemented. GET /v1/payout/{id} is a real
        NowPayments endpoint, confirmed empirically against a live
        payout in WITHDRAWAL-E2E-02A (HTTP 200, real status body) — it
        is purely read-only: never mutates anything provider-side, and
        is NOT the "Verify payout" 2FA action (out of scope — see
        WITHDRAWAL-E2E-02B design report).

        attempt.provider_reference is the individual payout id
        NowPayments assigned at creation (np_payout_id) — populated only
        once create_payout() succeeded, which is exactly the population
        reconcile_unknown_payout_attempts() calls this for (UNKNOWN
        attempts, i.e. ones that got at least as far as SUBMITTED).
        """
        if not attempt.provider_reference:
            return PayoutLookupResult(outcome=PayoutLookupOutcome.UNSUPPORTED)

        try:
            token = _np._get_jwt_token()
        except Exception as exc:
            logger.warning("[NP] lookup_payout auth failed attempt=%d: %s", attempt.pk, exc)
            return PayoutLookupResult(outcome=PayoutLookupOutcome.UNAVAILABLE)

        try:
            resp = requests.get(
                f"{_np._BASE}/payout/{attempt.provider_reference}",
                headers={"x-api-key": _np._api_key(), "Authorization": f"Bearer {token}"},
                timeout=15,
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            logger.warning("[NP] lookup_payout network error attempt=%d: %s", attempt.pk, exc)
            return PayoutLookupResult(outcome=PayoutLookupOutcome.UNAVAILABLE)

        if resp.status_code == 404:
            return PayoutLookupResult(outcome=PayoutLookupOutcome.NOT_FOUND)
        if not resp.ok:
            logger.warning("[NP] lookup_payout HTTP %d attempt=%d", resp.status_code, attempt.pk)
            return PayoutLookupResult(outcome=PayoutLookupOutcome.UNAVAILABLE)

        try:
            body = resp.json()
            withdrawals = body.get("withdrawals", [])
            wd = next(
                (w for w in withdrawals if str(w.get("id", "")) == attempt.provider_reference), None,
            ) or (withdrawals[0] if withdrawals else None)
            if wd is None:
                return PayoutLookupResult(outcome=PayoutLookupOutcome.AMBIGUOUS, raw_metadata=body)
            raw_status = str(wd.get("status", "")).upper()
        except (ValueError, AttributeError, TypeError):
            return PayoutLookupResult(outcome=PayoutLookupOutcome.AMBIGUOUS)

        outcome = _RAW_STATUS_TO_LOOKUP_OUTCOME.get(raw_status, PayoutLookupOutcome.AMBIGUOUS)
        return PayoutLookupResult(
            outcome=outcome,
            provider_reference=str(wd.get("id", "")),
            provider_batch_id=str(body.get("id", "")),
            raw_provider_status=raw_status,
            raw_metadata=wd,
        )

    def parse_webhook(self, raw_body: bytes, headers) -> list[ProviderPayoutEvent] | None:
        """
        Verifies the HMAC signature (nowpayments.verify_ipn_signature(),
        unmodified), then parses the payload into normalized events.
        Returns None on invalid signature or unparseable JSON (caller
        returns 400, identical to today's behavior). raw_status never
        crosses into PayoutAttempt.status directly — only
        normalized_status does.

        WITHDRAWAL-E2E-02G FASE B — supports BOTH real NowPayments
        payout IPN shapes, distinguished by inspection, never guessed:

          A) batch payload — {"id": batch_id, "withdrawals": [...]}.
             One event per withdrawals[] entry. This is the shape our
             own POST /payout *response* uses, and the shape this
             method originally (and wrongly) assumed was also the async
             IPN shape.

          B) individual/flat payload — the withdrawal's own fields at
             the payload's top level: {"batch_withdrawal_id": ...,
             "status"/"payout_status": ..., "id": ..., "hash": ..., ...}
             — confirmed CONFIRMADO against the official NowPaymentsIO
             Node.js SDK (nowpayments-sdk-nodejs, src/ipn.js
             normalizeWebhook()), whose own payout-webhook detection is
             exactly `'batch_withdrawal_id' in payload and ('status' in
             payload or 'payout_status' in payload)` with no array
             unwrapping. WR18's four real, HMAC-valid IPN deliveries
             were this shape — the pre-02G code's withdrawals[]-only
             assumption silently produced zero events for every one of
             them (WITHDRAWAL-E2E-02F/02G).

        WITHDRAWAL-E2E-02B — a raw_status NOT in _RAW_STATUS_TO_NORMALIZED
        no longer means "drop this event" (the old `continue`, which is
        exactly what silently discarded WR17's two real REJECTED
        webhooks — see WITHDRAWAL-E2E-02A). Every signature-valid entry
        now produces an event, with normalized_status=None when
        unrecognized — an honest "we don't know" the caller
        (payout_orchestrator.py) must persist and route to MANUAL_REVIEW,
        never guess a transition from.
        """
        sig = headers.get("x-nowpayments-sig", "")
        if not _np.verify_ipn_signature(raw_body, sig):
            return None

        try:
            data = json.loads(raw_body)
        except (json.JSONDecodeError, TypeError):
            return None

        occurred_at = timezone.now()

        if isinstance(data.get("withdrawals"), list):
            # Shape A — batch payload.
            batch_id = str(data.get("id", ""))
            events: list[ProviderPayoutEvent] = []
            for wd in data["withdrawals"]:
                raw_status = str(wd.get("status", "")).upper()
                normalized = _RAW_STATUS_TO_NORMALIZED.get(raw_status)  # None if unrecognized — never dropped
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

        if "batch_withdrawal_id" in data and ("status" in data or "payout_status" in data):
            # Shape B — individual/flat payload (the real async IPN shape).
            raw_status = str(data.get("status") or data.get("payout_status") or "").upper()
            normalized = _RAW_STATUS_TO_NORMALIZED.get(raw_status)  # None if unrecognized — never dropped
            return [ProviderPayoutEvent(
                provider=self.provider_name,
                provider_reference=str(data.get("id", "")),
                provider_batch_id=str(data.get("batch_withdrawal_id", "")),
                normalized_status=normalized,
                raw_status=raw_status,
                provider_amount=None,
                occurred_at=occurred_at,
                raw_event_payload=data,
            )]

        # Signature-valid but neither recognizable shape — do not invent
        # a structure NowPayments didn't send. No events extracted, but
        # (unlike a silent empty return) this is now visible in logs.
        logger.warning(
            "[NowPaymentsAdapter] signature-valid webhook body matched neither "
            "the batch nor the individual/flat payout shape — 0 events extracted"
        )
        return []


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
