"""
simulator/broker_audit.py
AUDIT-01 — Broker Event Audit Trail Foundation.

This is the broker's institutional, cross-engine chronological event
record — not a logger, not a log file. Every call here writes exactly
one durable, queryable BrokerAuditEvent row so that any trade, risk
decision, or admin action can be reconstructed step by step after the
fact, independent of whatever happens to be in the server log at the
time.

FASE 1 audit (read-only, no modifications made to any of the modules
below) found four pre-existing, narrow, single-purpose "event-ish"
records already in this codebase, none of which this module replaces:
    - simulator/audit.py's AuditLog (Phase B) — HTTP-request-scoped
      security events (logins, deposits, withdrawals, admin panel
      actions). Remains the system of record for its own domain;
      BrokerAuditEvent is not called from any of AuditLog's existing
      call sites.
    - simulator/broker_ledger.py's BrokerLedger (BOOK-02) — accounting
      rows with a Decimal amount (commission, spread, counterparty
      PnL). Remains the system of record for broker revenue; this
      module reads nothing from it and never duplicates its rows.
    - simulator/risk_engine.py's TradingViolation — per-account RiskRule
      breaches (MAX_DRAWDOWN, MAX_DAILY_LOSS, ...), a narrower, older
      concept than RISK-02's broker-wide limits. Untouched.
    - LedgerEntry — the trader-facing general ledger. Untouched.

The gap AUDIT-01 closes: position open, position close (all four real
close writers — WebSocket, daemon, admin force-close, population
engine), RISK-02 order rejections, and RISK-03 critical/high alerts
currently leave either NO durable record at all, or only a scrolling
text log line (`log.info("[db_open] REJECTED ...")`) that nothing can
query later. Admin force-close in particular writes a Trade, a
LedgerEntry, and a BrokerLedger row today, but never records WHO
(which staff user) triggered it — see the ENTREGA report's FASE 1
section for the full audit.

Design discipline carried over from BOOK-02/RISK-02/RISK-03:
    - record_event() never raises — an audit-write failure must never
      block a financial operation, exactly like audit.py's log_audit().
    - record_event() writes inside its own nested savepoint
      (transaction.atomic()) so that when called from inside an
      already-open outer transaction (e.g. _db_open_position_atomic),
      an audit-write failure cannot poison that outer transaction —
      the same nested-savepoint pattern consumers.py already uses for
      the BrokerLedger SPREAD insert.
    - Additive only: FASE 6 integrates exactly four call sites, chosen
      to cover every example in the block's own spec without recording
      the same real-world event twice (see the ENTREGA report for why
      "Position CLOSE" and "Ledger entry importante" are deliberately
      recorded as ONE event, not two, at BOOK-02's single close writer).

Post-review correction — the dashboard must never write:
    - observe_broker_alerts() is the ONLY sanctioned entry point for
      persisting RISK-03 alert observations. It is called exclusively
      from tasks.py's observe_broker_risk_alerts_task (a periodic Celery
      task — see CELERY_BEAT_SCHEDULE in settings.py), never from
      admin.py's _compute_control_data(). Loading or polling the Broker
      Control Center is a pure read of whatever has already been
      persisted; it cannot itself create a BrokerAuditEvent row.
    - record_alert_event()'s dedup check-then-create is now serialized
      by BrokerAuditObservationLock (a select_for_update() singleton
      mutex, same pattern as RISK-02's BrokerRiskLock) so two concurrent
      callers — e.g. two Celery workers overlapping on the same tick —
      cannot both pass the "not yet recorded" check and duplicate an
      observation. A prior review of this codebase explicitly rejected a
      Redis/cache-only lock for this exact class of problem in RISK-02;
      this follows that same precedent rather than reaching for one here.
    - Naming honesty: what this module records for a RISK-03 alert is a
      periodic OBSERVATION of currently-active alert state, not a
      lifecycle event — there is no persisted RAISED/CHANGED/RESOLVED
      state machine (RISK-03's BrokerRiskAlert is deliberately stateless,
      recomputed fresh every call). EV_RISK_ALERT_OBSERVED reflects that
      honestly; it does not claim an alert was newly "generated" simply
      because this module is the one that happened to look.

Post-review correction — financial vs. administrative events are
distinct, not duplicates:
    - broker_ledger.py's create_broker_counterparty_entry() remains the
      single writer of the canonical FINANCIAL event (EV_POSITION_CLOSED
      or a close-reason-specific subtype) — one per Trade, describing
      the fact that a position closed and what the broker's counterparty
      result was. It has no access to a staff actor and does not try to
      record one.
    - When the close reason is "admin_force_close", admin.py's own
      force_close view — the only code path that actually holds
      `request.user` — additionally records EV_ADMIN_POSITION_FORCE_CLOSE
      (category=ADMIN, actor_type=STAFF, actor_id=the real staff user),
      via record_admin_event(). This is a second, complementary event
      describing the human operational action, not a duplicate of the
      financial fact.
"""
from __future__ import annotations

import logging
import uuid as _uuid
from datetime import timedelta
from typing import Optional

from django.db import transaction
from django.utils import timezone

log = logging.getLogger("simulator.broker_audit")

_SOURCE = "simulator.broker_audit"


# ─────────────────────────────────────────────────────────────────────────
# FASE 3 — Category (no magic strings: always reference Category.X)
# ─────────────────────────────────────────────────────────────────────────
class Category:
    TRADING        = "TRADING"
    RISK           = "RISK"
    LEDGER         = "LEDGER"
    PAYMENTS       = "PAYMENTS"
    ADMIN          = "ADMIN"
    AUTHENTICATION = "AUTHENTICATION"
    COMPLIANCE     = "COMPLIANCE"
    MONITORING     = "MONITORING"
    SYSTEM         = "SYSTEM"
    # BOOK-04d — business decisions of the Routing Engine. Deliberately
    # distinct from MONITORING (infrastructure/providers/circuit-breakers/
    # health in market_data/) — a routing decision is a business fact
    # about an order, not an observation about system health.
    ROUTING        = "ROUTING"

    ALL = (TRADING, RISK, LEDGER, PAYMENTS, ADMIN, AUTHENTICATION,
           COMPLIANCE, MONITORING, SYSTEM, ROUTING)


# ─────────────────────────────────────────────────────────────────────────
# FASE 4 — Severity
# ─────────────────────────────────────────────────────────────────────────
class Severity:
    INFO     = "INFO"
    WARNING  = "WARNING"
    HIGH     = "HIGH"
    CRITICAL = "CRITICAL"

    ORDER = (INFO, WARNING, HIGH, CRITICAL)   # ascending
    _RANK = {name: i for i, name in enumerate(ORDER)}

    @classmethod
    def rank(cls, severity: str) -> int:
        return cls._RANK[severity]


# ─────────────────────────────────────────────────────────────────────────
# Actor — who/what caused the event
# ─────────────────────────────────────────────────────────────────────────
class ActorType:
    TRADER = "TRADER"   # the account holder, via the trading WebSocket
    STAFF  = "STAFF"    # a staff/admin user, via the admin console
    SYSTEM = "SYSTEM"   # an automated engine — daemon, risk engine, alert engine

    ALL = (TRADER, STAFF, SYSTEM)


# ─────────────────────────────────────────────────────────────────────────
# Event type constants — dot-namespaced, mirroring audit.py's own
# EV_* convention so the two systems read consistently side by side.
# ─────────────────────────────────────────────────────────────────────────
EV_POSITION_OPENED = "position.opened"

EV_POSITION_CLOSED               = "position.closed"
EV_POSITION_CLOSED_MANUAL        = "position.closed.manual"
EV_POSITION_CLOSED_STOP_LOSS     = "position.closed.stop_loss"
EV_POSITION_CLOSED_TAKE_PROFIT   = "position.closed.take_profit"
EV_POSITION_CLOSED_STOPOUT       = "position.closed.stopout"
EV_POSITION_CLOSED_MARGIN_CALL   = "position.closed.margin_call"
EV_POSITION_CLOSED_ADMIN         = "position.closed.admin_force_close"

EV_RISK_ORDER_REJECTED = "risk.order_rejected"

# Naming honesty (post-review correction) — this is a periodic
# OBSERVATION of currently-active alert state (collect_risk_alerts() is
# stateless, recomputed fresh every call), not a lifecycle event. There
# is no persisted RAISED/CHANGED/RESOLVED state machine yet, so the name
# must not claim one.
EV_RISK_ALERT_OBSERVED = "risk.alert_observed"

# Administrative action event — distinct from, and complementary to, the
# EV_POSITION_CLOSED_ADMIN financial event. Recorded only from admin.py's
# force_close view, which is the only code path holding the real staff
# actor (request.user). See the module docstring's "financial vs.
# administrative events" note.
EV_ADMIN_POSITION_FORCE_CLOSE = "admin.position_force_close"

# Close reason (from Trade/Position close writers) -> specific event_type.
# Any reason not in this map falls back to the generic EV_POSITION_CLOSED.
_CLOSE_REASON_EVENT_TYPE = {
    "manual":              EV_POSITION_CLOSED_MANUAL,
    "sl":                  EV_POSITION_CLOSED_STOP_LOSS,
    "daemon_sl":           EV_POSITION_CLOSED_STOP_LOSS,
    "tp":                  EV_POSITION_CLOSED_TAKE_PROFIT,
    "daemon_tp":           EV_POSITION_CLOSED_TAKE_PROFIT,
    "stopout":             EV_POSITION_CLOSED_STOPOUT,
    "daemon_stopout":      EV_POSITION_CLOSED_STOPOUT,
    "daemon_margin_call":  EV_POSITION_CLOSED_MARGIN_CALL,
    "admin_force_close":   EV_POSITION_CLOSED_ADMIN,
}

# ─────────────────────────────────────────────────────────────────────────
# AUDIT-02 — Payments & Payout Audit Trail.
#
# Two independent additions, both Category.PAYMENTS:
#   1. Funded payouts (H.2 FUNDED_SIM, H.3 FUNDED_INTERNAL) — previously had
#      ZERO durable cross-cutting record of any kind; only the FundedPayoutRequest
#      row's own reviewed_by/reviewed_at fields (last state, no history).
#   2. Deposit — the procedural moments (deposit.created, deposit.callback)
#      stay AuditLog-only (audit.py) — they are not yet a confirmed financial
#      fact. The financial moment (deposit.credited) is additionally mirrored
#      here, alongside the AuditLog row that already existed, closing the
#      asymmetry that funded payouts (money out) had institutional coverage
#      while deposits (money in) did not.
#
# Withdrawal (WithdrawalRequest) mirroring is deliberately OUT of scope here
# — it already has rich AuditLog coverage (request/approved/rejected/failed/
# refunded/completed); converging that into BrokerAuditEvent too is its own
# future block (see docs/AUDIT_TRAIL_ENGINE_PLAN.md — AUDIT-09).
# ─────────────────────────────────────────────────────────────────────────
EV_FUNDED_PAYOUT_SIM_APPROVED           = "payment.funded_payout_sim_approved"
EV_FUNDED_PAYOUT_INTERNAL_APPROVED      = "payment.funded_payout_internal_approved"
EV_FUNDED_PAYOUT_INTERNAL_SUBMITTED     = "payment.funded_payout_internal_submitted"
EV_FUNDED_PAYOUT_INTERNAL_SUBMIT_FAILED = "payment.funded_payout_internal_submit_failed"
EV_FUNDED_PAYOUT_INTERNAL_COMPLETED     = "payment.funded_payout_internal_completed"
EV_FUNDED_PAYOUT_INTERNAL_FAILED        = "payment.funded_payout_internal_failed"

# Mirrors audit.py's EV_DEPOSIT_CREDITED string exactly (same real-world
# fact, two systems, no import coupling between audit.py and broker_audit.py
# — each module owns its own constants, same as everywhere else in this file).
EV_DEPOSIT_CREDITED = "deposit.credited"


# ─────────────────────────────────────────────────────────────────────────
# AUDIT-03 — Compliance Audit Trail (KYC).
#
# Category.COMPLIANCE. Three events:
#   - kyc_approved / kyc_rejected — admin.py's approve_kyc()/reject_kyc(),
#     now re-locked with select_for_update() + a status recheck inside the
#     transaction (FASE 1 found these unlocked — two concurrent clicks on
#     the same profile could silently double-process it, overwriting
#     reviewed_by and double-queuing the notification email).
#   - kyc_resubmitted — views.py's kyc_view(), fired ONLY on the
#     REJECTED -> PENDING transition. This is the one moment
#     kyc.reviewed_at/reviewed_by/rejection_reason are wiped by the user's
#     own resubmission (test_kyc_ui.py::test_rejected_post_clears_review_
#     fields already proves this is deliberate, pre-existing behavior) —
#     kyc_resubmitted does not need to preserve that data itself, because
#     the EARLIER kyc_rejected event already recorded who rejected it, when,
#     and why, permanently, in its own row. kyc_resubmitted only needs to
#     record that the wipe-triggering transition happened, and to whom.
#     status_before is captured BEFORE the wipe (read from the locked row);
#     the event itself is written AFTER kyc.save(), inside the same
#     transaction — recording "what happened" is not the same as recording
#     it before the data disappears from KYCProfile (it already disappeared
#     from KYCProfile the moment .save() ran; it never disappears from
#     BrokerAuditEvent, which is the point of this module).
# ─────────────────────────────────────────────────────────────────────────
EV_KYC_APPROVED     = "compliance.kyc_approved"
EV_KYC_REJECTED     = "compliance.kyc_rejected"
EV_KYC_RESUBMITTED  = "compliance.kyc_resubmitted"

# Metadata whitelist enforced by the three call sites (admin.py, views.py) —
# never document_front/back/selfie, document_number, legal_name, country, or
# any raw file path. rejection_reason is truncated to this many characters.
KYC_REJECTION_REASON_MAX_LENGTH = 500


# ─────────────────────────────────────────────────────────────────────────
# AUDIT-04a — 2FA Lifecycle Trail.
#
# Category.AUTHENTICATION, first real call sites (dormant since AUDIT-01).
#   - 2fa_enabled  — views.py's totp_setup_view(). No lock needed: two
#     concurrent enables are two legitimately distinct attempts (e.g. a
#     retry after a mis-scanned QR), not a duplication bug — OneToOneField
#     already guarantees exactly one TOTPDevice row regardless. metadata
#     captures had_existing_device/previous_confirmed_at so a silent
#     re-enable (replacing an already-confirmed device's secret) leaves a
#     trace, even though no schema change was needed to record it.
#   - 2fa_disabled — views.py's totp_disable_view(). Locked
#     (transaction.atomic()+select_for_update()) because device.delete()
#     destroys the only record TOTPDevice ever held (confirmed_at) — two
#     concurrent disables (or a disable racing the emergency command below)
#     must not both succeed and both write an event for what is really one
#     fact. was_confirmed_at is captured BEFORE the delete.
#   - 2fa_disabled_emergency — disable_2fa management command. Same lock,
#     for the cross-flow race against a user's own self-service disable.
#     actor_type=SYSTEM, no actor_id — the command does not capture the
#     real human operator's identity (a known, accepted limitation, not
#     fixed by this block).
#   - 2fa_verify_failed — views.py's totp_verify_view(). Deliberately NOT
#     recorded on every failed attempt (those stay in security_log() only,
#     matching auth.login_failed's existing precedent) — BrokerAuditEvent
#     is reserved for institutional events, not a mirror of every brute-
#     force guess. Recorded only when a configurable threshold of
#     consecutive failures is crossed, using ratelimit.py's existing
#     Redis counter (rate_check()) in OBSERVE-ONLY mode — it counts, it
#     never blocks the retry. Fail-open of Redis itself (rate_check
#     returns (True, 0) on Redis failure) means the threshold simply never
#     fires; that is the same accepted behavior every other rate_check()
#     caller already has.
# ─────────────────────────────────────────────────────────────────────────
EV_2FA_ENABLED             = "auth.2fa_enabled"
EV_2FA_DISABLED            = "auth.2fa_disabled"
EV_2FA_DISABLED_EMERGENCY  = "auth.2fa_disabled_emergency"
EV_2FA_VERIFY_FAILED       = "auth.2fa_verify_failed"

from django.conf import settings as _settings  # noqa: E402

AUDIT04A_2FA_VERIFY_FAIL_THRESHOLD = int(
    getattr(_settings, "AUDIT04A_2FA_VERIFY_FAIL_THRESHOLD", 5)
)
AUDIT04A_2FA_VERIFY_FAIL_WINDOW_SECONDS = int(
    getattr(_settings, "AUDIT04A_2FA_VERIFY_FAIL_WINDOW_SECONDS", 300)
)


# ─────────────────────────────────────────────────────────────────────────
# AUDIT-04b — Password & Admin Access Trail.
#
# Category.AUTHENTICATION, same wrapper (record_auth_event()) as AUDIT-04a
# — no new wrapper needed. Five events, all in simulator/auth_password_
# views.py (thin subclasses of Django's own password change/reset CBVs —
# never reimplement password hashing, token generation, or token
# validation) except the two admin_site_login_* events, which live in
# admin.py's MoneyBrokerAdminSite.login() override.
#
#   - password_changed             — no lock needed (single mutation, no
#     state machine; two concurrent changes are two legitimate facts).
#   - password_reset_requested     — fired from a PasswordResetForm
#     subclass's send_mail() override (called once per matched user,
#     AFTER Django's own send succeeds) — never fired for a non-existent
#     email, and the fork happens entirely server-side so the HTTP
#     response never reveals whether a user matched.
#   - password_reset_completed     — fired from PasswordResetConfirmView's
#     form_valid(), only after super().form_valid() succeeds.
#   - admin_site_login_success/failed — MoneyBrokerAdminSite.login()
#     override, observing Django's own AdminSite.login() outcome.
#
# correlation_id semantics (deliberately NOT "one id per request"):
# Django's default_token_generator produces a DETERMINISTIC token from
# (user pk, password hash, last_login, a rotating time-hash) — two reset
# requests for the same user within the same token/timestamp bucket, with
# nothing about the user changed in between, produce the IDENTICAL token
# string. correlation_id is therefore keyed by sha256(token), not by
# request or by user: multiple requests that happen to produce the same
# token share the same correlation_id (correct — they ARE the same
# reset attempt as far as the token/link is concerned). A request that
# produces a different token (state changed, or enough time passed) gets
# its own correlation_id. See _get_or_create_correlation() in
# auth_password_views.py for the atomic Redis SET NX EX implementation —
# first writer for a given token wins and defines that token's
# correlation_id permanently (within its TTL); every subsequent caller
# for the same token reads that same value back, never overwrites it.
# ─────────────────────────────────────────────────────────────────────────
EV_PASSWORD_CHANGED           = "auth.password_changed"
EV_PASSWORD_RESET_REQUESTED   = "auth.password_reset_requested"
EV_PASSWORD_RESET_COMPLETED   = "auth.password_reset_completed"
EV_ADMIN_SITE_LOGIN_SUCCESS   = "auth.admin_site_login_success"
EV_ADMIN_SITE_LOGIN_FAILED    = "auth.admin_site_login_failed"

# Matches User.username's own max_length — never store more of an
# attempted admin login username than Django itself would ever accept.
ADMIN_LOGIN_USERNAME_ATTEMPTED_MAX_LENGTH = 150


# RISK-03 severities this module will record as an audit event when a
# collector produces one — INFO/LOW/MEDIUM are deliberately not recorded
# here (FASE 6: "Integrar solamente en eventos críticos").
_ALERT_SEVERITY_MAP = {
    "CRITICAL": Severity.CRITICAL,
    "HIGH":     Severity.HIGH,
}

# How long a given RISK-03 alert_id is considered "already recorded" —
# collect_risk_alerts() is stateless and recomputed on every
# observe_broker_alerts() run, so without this window the same
# still-active alert would create a fresh audit row every run. Overridable
# via Django settings, same pattern as every other AUDIT-01/RISK-0x
# threshold.
from django.conf import settings as _settings  # noqa: E402

ALERT_DEDUP_WINDOW_SECONDS = int(
    getattr(_settings, "AUDIT01_ALERT_DEDUP_WINDOW_SECONDS", 900)
)


def close_reason_event_type(reason: str) -> str:
    """Maps a close `reason` string (as already used by every close
    writer in the codebase) to the specific EV_POSITION_CLOSED_* event
    type, or the generic EV_POSITION_CLOSED if the reason is unrecognized
    — never raises on an unknown reason."""
    return _CLOSE_REASON_EVENT_TYPE.get((reason or "").lower(), EV_POSITION_CLOSED)


# ─────────────────────────────────────────────────────────────────────────
# BOOK-04d — Routing Engine audit trail. A single, reusable event type —
# same shape whether the call opened a brand-new Position or merged into
# an existing one (merged=True/False lives in metadata, mirroring how
# EV_POSITION_OPENED already distinguishes the two cases in its own
# metadata rather than using two different event types).
# ─────────────────────────────────────────────────────────────────────────
EV_ROUTING_DECISION_RECORDED = "routing.decision_recorded"


# ─────────────────────────────────────────────────────────────────────────
# FASE 5 — the engine. record_event() is the single writer; every other
# record_*_event() function is a thin, category-fixed convenience
# wrapper around it, so the actual database write exists in exactly one
# place (the same "single writer" discipline as BOOK-02's
# create_broker_counterparty_entry()).
# ─────────────────────────────────────────────────────────────────────────
def record_event(
    *,
    event_type: str,
    category: str,
    severity: str,
    actor_type: str,
    description: str,
    actor_id: Optional[int] = None,
    account_id: Optional[int] = None,
    account=None,
    trade_id: Optional[int] = None,
    trade=None,
    funded_payout_request_id: Optional[int] = None,
    funded_payout_request=None,
    deposit_id: Optional[int] = None,
    deposit=None,
    user_id: Optional[int] = None,
    user=None,
    correlation_id=None,
    event_version: int = 1,
    symbol: str = "",
    metadata: Optional[dict] = None,
    source_module: str = "",
    request=None,
    request_id: Optional[str] = None,
):
    """
    Writes exactly one BrokerAuditEvent row. Never raises — an audit
    write failure is logged and swallowed, never allowed to block or
    roll back the caller's own transaction (matches audit.py's
    log_audit() contract exactly). This fail-open behavior is the
    Audit Trail Engine's non-negotiable default — see the module docstring
    of docs/AUDIT_TRAIL_ENGINE_PLAN.md §9: any future fail-closed exception
    must be justified explicitly in its own block's design, never introduced
    silently here.

    Writes inside a nested savepoint (transaction.atomic()) so a failure
    here can never poison an outer atomic() block the caller may already
    be inside — same pattern as the BrokerLedger SPREAD insert in
    consumers.py.

    AUDIT-02 additions (all optional, retrocompatible — existing callers
    from AUDIT-01 pass none of them and are unaffected):
      funded_payout_request_id/funded_payout_request — Payments domain FK.
      deposit_id/deposit                             — Payments domain FK.
      correlation_id — institutional trace id spanning this operation's
          full lifecycle across however many requests/tasks touched it.
          Distinct from request_id (below), which only correlates events
          within a single HTTP request/Celery task. Read back from the
          root entity (FundedPayoutRequest.correlation_id,
          Deposit.correlation_id) by the caller — never generated here.
      event_version — versions this event_type's `metadata` shape only
          (see module docstring). Defaults to 1 for every event_type.

    AUDIT-03 addition (also optional, retrocompatible):
      user_id/user — the SUBJECT of the event (e.g. the KYCProfile owner),
          distinct from actor_id (who performed the action). Needed for
          domains anchored to User rather than TradingAccount.
    """
    try:
        from .models import BrokerAuditEvent

        if request_id is None:
            try:
                from .observability import get_request_id
                request_id = (getattr(request, "request_id", None) or get_request_id() or "") if request else ""
            except Exception:
                request_id = ""

        with transaction.atomic():
            event = BrokerAuditEvent.objects.create(
                event_id=_uuid.uuid4(),
                event_type=event_type,
                category=category,
                severity=severity,
                actor_type=actor_type,
                actor_id=actor_id,
                account_id=account.id if account is not None else account_id,
                trade_id=trade.id if trade is not None else trade_id,
                funded_payout_request_id=(
                    funded_payout_request.id if funded_payout_request is not None
                    else funded_payout_request_id
                ),
                deposit_id=deposit.id if deposit is not None else deposit_id,
                user_id=user.id if user is not None else user_id,
                correlation_id=correlation_id,
                event_version=event_version,
                symbol=symbol or "",
                description=description,
                metadata=metadata or {},
                source_module=source_module or _SOURCE,
                request_id=request_id or "",
            )
        log.info(
            "[broker_audit] event=%s category=%s severity=%s account=%s symbol=%s",
            event_type, category, severity, event.account_id, symbol,
        )
        return event
    except Exception as exc:
        log.error("[broker_audit] FAILED to record event=%s: %r", event_type, exc, exc_info=True)
        return None


def record_trade_event(
    *, event_type: str, severity: str = Severity.INFO, actor_type: str = ActorType.TRADER,
    description: str, account_id=None, account=None, trade_id=None, trade=None,
    symbol: str = "", metadata: Optional[dict] = None, source_module: str = "",
    request=None,
):
    """FASE 5 — TRADING category convenience wrapper (position open/close)."""
    return record_event(
        event_type=event_type, category=Category.TRADING, severity=severity,
        actor_type=actor_type, description=description,
        account_id=account_id, account=account, trade_id=trade_id, trade=trade,
        symbol=symbol, metadata=metadata, source_module=source_module, request=request,
    )


def record_risk_event(
    *, event_type: str, severity: str = Severity.HIGH, actor_type: str = ActorType.SYSTEM,
    description: str, account_id=None, account=None, symbol: str = "",
    metadata: Optional[dict] = None, source_module: str = "",
):
    """FASE 5 — RISK category convenience wrapper (RISK-02 rejections, RISK-03 alerts)."""
    return record_event(
        event_type=event_type, category=Category.RISK, severity=severity,
        actor_type=actor_type, description=description,
        account_id=account_id, account=account, symbol=symbol,
        metadata=metadata, source_module=source_module,
    )


def record_routing_event(
    *, event_type: str, severity: str = Severity.INFO, actor_type: str = ActorType.SYSTEM,
    description: str, account_id=None, account=None, symbol: str = "",
    metadata: Optional[dict] = None, source_module: str = "",
):
    """
    BOOK-04d — ROUTING category convenience wrapper. Same single-writer
    discipline as every other record_*_event(): delegates 100% to
    record_event(), no logic of its own.

    actor_type defaults to SYSTEM — a routing decision is the engine's
    own determination, not a trader or staff action, same rationale as
    record_risk_event()'s default.

    Callers (see simulator/consumers.py) must keep `metadata` thin —
    routing_decision_id/position_id/merged only, per
    docs/BOOK_04_IMPLEMENTATION_PLAN.md's BOOK-04d contract — never
    inputs_snapshot, book, reason_code, or engine_version, none of which
    are available without re-reading RoutingDecision (explicitly
    forbidden by that same contract).
    """
    return record_event(
        event_type=event_type, category=Category.ROUTING, severity=severity,
        actor_type=actor_type, description=description,
        account_id=account_id, account=account, symbol=symbol,
        metadata=metadata, source_module=source_module,
    )


def record_admin_event(
    *, event_type: str, severity: str = Severity.WARNING, description: str,
    actor_id: Optional[int] = None, account_id=None, account=None,
    trade_id=None, trade=None, symbol: str = "", metadata: Optional[dict] = None,
    source_module: str = "", request=None,
):
    """FASE 5 — ADMIN category convenience wrapper (staff-initiated actions)."""
    return record_event(
        event_type=event_type, category=Category.ADMIN, severity=severity,
        actor_type=ActorType.STAFF, description=description, actor_id=actor_id,
        account_id=account_id, account=account, trade_id=trade_id, trade=trade,
        symbol=symbol, metadata=metadata, source_module=source_module, request=request,
    )


def record_payment_event(
    *, event_type: str, severity: str = Severity.INFO, actor_type: str = ActorType.STAFF,
    description: str, actor_id: Optional[int] = None, account_id=None, account=None,
    funded_payout_request_id=None, funded_payout_request=None,
    deposit_id=None, deposit=None, correlation_id=None,
    metadata: Optional[dict] = None, source_module: str = "",
):
    """AUDIT-02 — PAYMENTS category convenience wrapper (funded payouts,
    deposit.credited mirror). Same single-writer discipline as every other
    record_*_event(): this delegates to record_event(), it never writes
    directly."""
    return record_event(
        event_type=event_type, category=Category.PAYMENTS, severity=severity,
        actor_type=actor_type, description=description, actor_id=actor_id,
        account_id=account_id, account=account,
        funded_payout_request_id=funded_payout_request_id,
        funded_payout_request=funded_payout_request,
        deposit_id=deposit_id, deposit=deposit, correlation_id=correlation_id,
        metadata=metadata, source_module=source_module,
    )


def record_compliance_event(
    *, event_type: str, severity: str = Severity.WARNING, actor_type: str = ActorType.STAFF,
    description: str, actor_id: Optional[int] = None,
    user_id: Optional[int] = None, user=None,
    metadata: Optional[dict] = None, source_module: str = "",
):
    """AUDIT-03 — COMPLIANCE category convenience wrapper (KYC approve/
    reject/resubmit). Same single-writer discipline as every other
    record_*_event(): delegates 100% to record_event(), no logic of its own.
    No correlation_id parameter — KYCProfile is a reused row across a
    user's whole lifetime (OneToOneField), not a fresh-per-operation entity
    like Deposit/FundedPayoutRequest, so a correlation_id here would be
    constant forever and redundant with user_id. The "reference to the
    prior rejection" this domain needs is answered by events_for_user()
    ordered by timestamp, not by a stored pointer."""
    return record_event(
        event_type=event_type, category=Category.COMPLIANCE, severity=severity,
        actor_type=actor_type, description=description, actor_id=actor_id,
        user_id=user_id, user=user,
        metadata=metadata, source_module=source_module,
    )


def record_auth_event(
    *, event_type: str, severity: str = Severity.INFO, actor_type: str = ActorType.TRADER,
    description: str, actor_id: Optional[int] = None,
    user_id: Optional[int] = None, user=None,
    correlation_id=None,
    metadata: Optional[dict] = None, source_module: str = "",
):
    """AUTHENTICATION category convenience wrapper. Same single-writer
    discipline as every other record_*_event(): delegates 100% to
    record_event(), no logic of its own.

    correlation_id is optional and unused by AUDIT-04a's 2FA lifecycle
    events (atomic, single-request, nothing to correlate). AUDIT-04b's
    password_reset_requested/completed DO use it — see their module
    (auth_password_views.py) for the sha256(token)-keyed semantics."""
    return record_event(
        event_type=event_type, category=Category.AUTHENTICATION, severity=severity,
        actor_type=actor_type, description=description, actor_id=actor_id,
        user_id=user_id, user=user, correlation_id=correlation_id,
        metadata=metadata, source_module=source_module,
    )


def record_system_event(
    *, event_type: str, category: str = Category.SYSTEM, severity: str = Severity.INFO,
    description: str, account_id=None, account=None, symbol: str = "",
    metadata: Optional[dict] = None, source_module: str = "",
):
    """FASE 5 — SYSTEM category convenience wrapper (background/automated events)."""
    return record_event(
        event_type=event_type, category=category, severity=severity,
        actor_type=ActorType.SYSTEM, description=description,
        account_id=account_id, account=account, symbol=symbol,
        metadata=metadata, source_module=source_module,
    )


def record_alert_event(alert, *, dedup_window_seconds: Optional[int] = None):
    """
    FASE 6 — records a RISK-03 BrokerRiskAlert as an audit OBSERVATION,
    but only for CRITICAL/HIGH severities and only if an observation for
    the same deterministic alert_id was not already recorded within the
    dedup window. Returns None (no-op, not a failure) if the alert's
    severity isn't audited or it was already recorded recently.

    Post-review correction: the dedup check-then-create is now held
    under BrokerAuditObservationLock (select_for_update()) for its
    entire duration — closing the TOCTOU race a plain "SELECT EXISTS
    then INSERT" has under concurrent callers (see the model's
    docstring). This function is not meant to be called from any
    request/dashboard code path — see observe_broker_alerts() below,
    the single sanctioned entry point.
    """
    from .models import BrokerAuditEvent, BrokerAuditObservationLock

    severity = _ALERT_SEVERITY_MAP.get(alert.severity)
    if severity is None:
        return None

    window = ALERT_DEDUP_WINDOW_SECONDS if dedup_window_seconds is None else dedup_window_seconds

    with transaction.atomic():
        if window > 0:
            BrokerAuditObservationLock.objects.get_or_create(pk=1)
            BrokerAuditObservationLock.objects.select_for_update().get(pk=1)

            cutoff = timezone.now() - timedelta(seconds=window)
            already_recorded = BrokerAuditEvent.objects.filter(
                event_type=EV_RISK_ALERT_OBSERVED,
                metadata__alert_id=alert.alert_id,
                timestamp__gte=cutoff,
            ).exists()
            if already_recorded:
                return None

        return record_risk_event(
            event_type=EV_RISK_ALERT_OBSERVED,
            severity=severity,
            description=alert.title,
            account_id=alert.affected_account,
            symbol=alert.affected_symbol or "",
            source_module=alert.source_module,
            metadata={
                "alert_id": alert.alert_id,
                "alert_category": alert.category,
                "metric": alert.metric,
                "current_value": float(alert.current_value) if alert.current_value is not None else None,
                "threshold": float(alert.threshold) if alert.threshold is not None else None,
            },
        )


def record_active_alerts(alerts, *, dedup_window_seconds: Optional[int] = None) -> int:
    """FASE 6 — convenience batch wrapper: records every CRITICAL/HIGH
    alert in `alerts` (typically the output of
    broker_alerts.collect_risk_alerts()) as an observation, applying the
    same per-alert dedup window to each. Returns the count of NEW rows
    actually written (skips both non-critical severities and dedup
    hits). Like record_alert_event(), not meant to be called from a
    request/dashboard code path directly — see observe_broker_alerts()."""
    written = 0
    for alert in alerts:
        if record_alert_event(alert, dedup_window_seconds=dedup_window_seconds) is not None:
            written += 1
    return written


def observe_broker_alerts(*, dedup_window_seconds: Optional[int] = None) -> int:
    """
    Post-review correction — the single sanctioned operational entry
    point for persisting RISK-03 alert observations. Pulls the current
    alert snapshot from broker_alerts.py (a pure read — that module's
    own documented contract is never touched by this call) and persists
    CRITICAL/HIGH observations via record_active_alerts().

    Called exclusively from tasks.py's observe_broker_risk_alerts_task
    (a periodic Celery task). NEVER call this from admin.py or any other
    request-handling code path — alert history must exist independent
    of whether any staff member has the dashboard open; a GET/poll must
    never be able to write to the audit trail. Returns the count of new
    rows written.
    """
    from .broker_alerts import collect_risk_alerts
    alerts = collect_risk_alerts()
    return record_active_alerts(alerts, dedup_window_seconds=dedup_window_seconds)


# ─────────────────────────────────────────────────────────────────────────
# FASE 7 — queries
# ─────────────────────────────────────────────────────────────────────────
def recent_events(limit: int = 50):
    """Most recent events across the whole broker, newest first."""
    from .models import BrokerAuditEvent
    return list(BrokerAuditEvent.objects.order_by("-timestamp", "-id")[:limit])


def events_for_account(account_id: int, limit: int = 50):
    from .models import BrokerAuditEvent
    return list(
        BrokerAuditEvent.objects.filter(account_id=account_id)
        .order_by("-timestamp", "-id")[:limit]
    )


def events_for_trade(trade_id: int, limit: int = 50):
    from .models import BrokerAuditEvent
    return list(
        BrokerAuditEvent.objects.filter(trade_id=trade_id)
        .order_by("-timestamp", "-id")[:limit]
    )


def events_for_symbol(symbol: str, limit: int = 50):
    from .models import BrokerAuditEvent
    return list(
        BrokerAuditEvent.objects.filter(symbol=symbol)
        .order_by("-timestamp", "-id")[:limit]
    )


def events_by_category(category: str, limit: int = 50):
    from .models import BrokerAuditEvent
    return list(
        BrokerAuditEvent.objects.filter(category=category)
        .order_by("-timestamp", "-id")[:limit]
    )


def events_by_severity(severity: str, limit: int = 50):
    from .models import BrokerAuditEvent
    return list(
        BrokerAuditEvent.objects.filter(severity=severity)
        .order_by("-timestamp", "-id")[:limit]
    )


# ─────────────────────────────────────────────────────────────────────────
# AUDIT-02 — Payments domain queries (same pattern as the six above).
# ─────────────────────────────────────────────────────────────────────────
def events_for_funded_payout(funded_payout_request_id: int, limit: int = 50):
    from .models import BrokerAuditEvent
    return list(
        BrokerAuditEvent.objects.filter(funded_payout_request_id=funded_payout_request_id)
        .order_by("-timestamp", "-id")[:limit]
    )


def events_for_deposit(deposit_id: int, limit: int = 50):
    from .models import BrokerAuditEvent
    return list(
        BrokerAuditEvent.objects.filter(deposit_id=deposit_id)
        .order_by("-timestamp", "-id")[:limit]
    )


def events_for_user(user_id: int, limit: int = 50):
    """
    AUDIT-03 — all events where `user` is the subject, across every
    category (not only COMPLIANCE). Callers wanting only the KYC history
    for a user should additionally filter .filter(category=Category.
    COMPLIANCE) — this function intentionally stays as generic as the other
    seven events_for_X()/events_by_X() helpers, not KYC-specific.
    """
    from .models import BrokerAuditEvent
    return list(
        BrokerAuditEvent.objects.filter(user_id=user_id)
        .order_by("-timestamp", "-id")[:limit]
    )


def events_by_correlation_id(correlation_id, limit: int = 200):
    """
    All events sharing the same institutional correlation_id, across every
    domain/FK they individually reference — the one query that answers
    "tell me the whole story of this operation" regardless of how many
    HTTP requests, Celery tasks, or webhooks it took.
    """
    from .models import BrokerAuditEvent
    return list(
        BrokerAuditEvent.objects.filter(correlation_id=correlation_id)
        .order_by("-timestamp", "-id")[:limit]
    )
