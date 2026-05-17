"""
simulator/tasks.py
Celery tasks — infrastructure layer ONLY.
Rules:
  - NO direct writes to TradingAccount, Position, Trade, or LedgerEntry
  - NO calls to trading engine, risk engine, or margin engine
  - Safe to retry, idempotent where possible
"""
import logging
from celery import shared_task
from django.conf import settings

logger = logging.getLogger("simulator.tasks")


# ──────────────────────────────────────────────────────
# PING — infrastructure health test
# ──────────────────────────────────────────────────────
@shared_task(name="simulator.ping", bind=True, max_retries=0)
def ping_task(self, payload: str = "pong") -> dict:
    """Simple round-trip task to verify workers are processing."""
    import time
    logger.info("[ping_task] worker=%s payload=%s", self.request.hostname, payload)
    return {
        "status": "ok",
        "payload": payload,
        "worker": self.request.hostname,
        "task_id": self.request.id,
        "timestamp": time.time(),
    }


# ──────────────────────────────────────────────────────
# EMAIL ASYNC — fire-and-forget email delivery
# ──────────────────────────────────────────────────────
@shared_task(
    name="simulator.send_email",
    bind=True,
    max_retries=3,
    default_retry_delay=60,        # retry after 60s
    autoretry_for=(Exception,),
    retry_backoff=True,
)
def send_email_async(
    self,
    subject: str,
    message: str,
    recipient_list: list[str],
    html_message: str | None = None,
    from_email: str | None = None,
) -> dict:
    """
    Send an email via Django's email backend.
    Drop-in replacement for synchronous send_mail() calls.
    """
    from django.core.mail import send_mail as _send
    from_email = from_email or settings.DEFAULT_FROM_EMAIL
    logger.info("[send_email_async] to=%s subject=%r", recipient_list, subject[:60])
    _send(
        subject=subject,
        message=message,
        from_email=from_email,
        recipient_list=recipient_list,
        html_message=html_message,
        fail_silently=False,
    )
    logger.info("[send_email_async] sent OK → %s", recipient_list)
    return {"sent": True, "recipients": recipient_list}


# ──────────────────────────────────────────────────────
# DEPOSIT RECONCILIATION — safe read-only audit
# ──────────────────────────────────────────────────────
@shared_task(
    name="simulator.reconcile_deposits",
    bind=True,
    max_retries=2,
    default_retry_delay=120,
)
def reconcile_deposits_task(self, hours_back: int = 24) -> dict:
    """
    Audit unconfirmed deposits older than `hours_back` hours.
    READ-ONLY: logs anomalies, does NOT modify wallet or ledger.
    A human (admin) must act on the log output.
    """
    from django.utils import timezone
    from datetime import timedelta
    from .models import Deposit

    cutoff = timezone.now() - timedelta(hours=hours_back)
    pending = Deposit.objects.filter(
        created_at__lte=cutoff,
        credited=False,
    ).values("id", "order_id", "amount_usd", "created_at")

    ids = [d["id"] for d in pending]
    if ids:
        logger.warning(
            "[reconcile_deposits] %d unconfirmed deposit(s) older than %dh: IDs=%s",
            len(ids), hours_back, ids,
        )
    else:
        logger.info("[reconcile_deposits] all deposits confirmed within %dh window", hours_back)

    return {"checked": True, "stale_count": len(ids), "stale_ids": ids}


# ──────────────────────────────────────────────────────
# WITHDRAWAL RECONCILIATION — safe read-only audit
# ──────────────────────────────────────────────────────
@shared_task(
    name="simulator.reconcile_withdrawals",
    bind=True,
    max_retries=2,
    default_retry_delay=120,
)
def reconcile_withdrawals_task(self, hours_back: int = 48) -> dict:
    """
    Audit pending/processing withdrawals stuck for too long.
    READ-ONLY: logs anomalies, does NOT modify wallet or ledger.
    """
    from django.utils import timezone
    from datetime import timedelta
    from .models import WithdrawalRequest

    cutoff = timezone.now() - timedelta(hours=hours_back)
    stuck = WithdrawalRequest.objects.filter(
        created_at__lte=cutoff,
        status__in=["pending", "processing"],
    ).values("id", "amount_usd", "status", "created_at")

    ids = [w["id"] for w in stuck]
    if ids:
        logger.warning(
            "[reconcile_withdrawals] %d stuck withdrawal(s) older than %dh: IDs=%s",
            len(ids), hours_back, ids,
        )
    else:
        logger.info("[reconcile_withdrawals] no stuck withdrawals in %dh window", hours_back)

    return {"checked": True, "stuck_count": len(ids), "stuck_ids": ids}


# ──────────────────────────────────────────────────────
# EQUITY SNAPSHOTS — time-series financial state capture
# ──────────────────────────────────────────────────────
@shared_task(
    name="simulator.take_snapshots",
    bind=True,
    max_retries=1,
    default_retry_delay=20,
    acks_late=True,          # only ack after the task completes (prevents duplicate snapshots)
    soft_time_limit=55,      # raises SoftTimeLimitExceeded before the next minute tick
    time_limit=90,           # hard kill — prevents a hung task from blocking the worker
)
def take_snapshots_task(self) -> dict:
    """
    Capture broker-wide + per-account equity state.
    Writes BrokerEquitySnapshot + AccountEquitySnapshot rows.
    No financial mutations — snapshot rows only.
    """
    from .snapshots import take_all_snapshots
    logger.info("[take_snapshots] starting worker=%s", self.request.hostname)
    result = take_all_snapshots()
    logger.info(
        "[take_snapshots] done broker_id=%s accounts=%d equity=%.2f",
        result.get("broker_snapshot_id"), result.get("account_snapshots", 0),
        result.get("total_equity", 0.0),
    )
    return result


@shared_task(
    name="simulator.cleanup_snapshots",
    bind=True,
    max_retries=1,
    default_retry_delay=60,
)
def cleanup_snapshots_task(self, retention_days: int | None = None) -> dict:
    """
    Delete equity snapshot rows older than retention_days (default: SNAPSHOT_RETENTION_DAYS env).
    Never touches financial data.
    """
    from .snapshots import cleanup_old_snapshots
    result = cleanup_old_snapshots(retention_days)
    logger.info(
        "[cleanup_snapshots] retention=%dd broker_del=%d account_del=%d",
        result["retention_days"], result["broker_snapshots_deleted"],
        result["account_snapshots_deleted"],
    )
    return result
