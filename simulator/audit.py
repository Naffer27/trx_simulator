"""
simulator/audit.py
Append-only audit trail helper.

Rules:
- ONLY writes to AuditLog — never reads, never modifies financial data
- Always called from views or tasks, never from model save()
- Never raises — a logging failure must not block a financial operation
- All arguments optional except event_type + action

Usage:
    from .audit import log_audit
    log_audit(request, "deposit.credited", "Deposit #42 credited $100",
              detail={"deposit_id": 42, "amount": "100.00", "wallet_before": "0.00"})
"""
import logging

from .observability import get_client_ip, get_request_id

_log = logging.getLogger("simulator.audit")

# ── Event type constants (import these at call sites for autocomplete + typo safety) ──
EV_AUTH_LOGIN_SUCCESS = "auth.login_success"
EV_AUTH_LOGIN_FAILED  = "auth.login_failed"

EV_DEPOSIT_CREATED   = "deposit.created"
EV_DEPOSIT_CREDITED  = "deposit.credited"
EV_DEPOSIT_CALLBACK  = "deposit.callback"

EV_WITHDRAW_REQUEST  = "withdrawal.requested"
EV_WITHDRAW_CALLBACK = "withdrawal.callback"
EV_WITHDRAW_APPROVED = "withdrawal.approved"
EV_WITHDRAW_REJECTED = "withdrawal.rejected"
EV_WITHDRAW_COMPLETE = "withdrawal.completed"
EV_WITHDRAW_FAILED   = "withdrawal.failed"
EV_WITHDRAW_REFUNDED = "withdrawal.refunded"

EV_ACCOUNT_FUNDED    = "account.funded"
EV_ACCOUNT_WITHDRAWN = "account.withdrawn"
EV_ACCOUNT_CREATED   = "account.created"

EV_ADMIN_ACTION      = "admin.action"
EV_ADMIN_VIEW        = "admin.view"


def log_audit(
    request,
    event_type: str,
    action: str,
    *,
    account=None,
    detail: dict | None = None,
) -> None:
    """
    Write one AuditLog row. Never raises.

    Args:
        request:    Django HttpRequest (or None for Celery/background tasks)
        event_type: Dot-separated category, e.g. "deposit.credited"
        action:     Human-readable label, e.g. "Deposit #42 credited $100"
        account:    TradingAccount FK (optional)
        detail:     JSON-serialisable dict with before/after state, IDs, amounts
    """
    try:
        from .models import AuditLog

        user       = getattr(request, "user", None) if request else None
        ip         = get_client_ip(request) if request else None
        endpoint   = request.path if request else ""
        method     = request.method if request else ""
        request_id = getattr(request, "request_id", None) or get_request_id() or ""

        # Resolve user FK — anonymous users have no pk
        user_obj = None
        if user and getattr(user, "is_authenticated", False):
            user_obj = user

        AuditLog.objects.create(
            event_type=event_type,
            action=action,
            user=user_obj,
            account=account,
            ip=ip,
            endpoint=endpoint,
            method=method,
            request_id=request_id,
            detail=detail or {},
        )
        _log.info(
            "[audit] event=%s action=%r user=%s account=%s",
            event_type, action,
            getattr(user_obj, "username", "anon"),
            getattr(account, "id", None),
        )
    except Exception as exc:
        # Audit failure must never break the calling request
        _log.error("[audit] FAILED to write audit log event=%s: %r", event_type, exc)
