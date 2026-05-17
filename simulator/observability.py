"""
simulator/observability.py
Sentry init, structured JSON logging, Celery signal hooks, Redis metrics helpers.
NO trading, wallet, or risk engine logic.
"""
import json
import logging
import os
import threading
import time as _time

# ── Thread-local request context ──────────────────────────────────────────────
_ctx = threading.local()


def set_request_id(rid: str) -> None:
    _ctx.request_id = rid


def get_request_id() -> str | None:
    return getattr(_ctx, "request_id", None)


# ── JSON log formatter ─────────────────────────────────────────────────────────
class StructuredFormatter(logging.Formatter):
    """Emits one compact JSON line per log record. Enable with LOG_JSON=true."""

    def format(self, record: logging.LogRecord) -> str:
        doc: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = get_request_id()
        if rid:
            doc["req_id"] = rid
        for key in ("task_name", "task_id", "user_id", "account_id"):
            val = getattr(record, key, None)
            if val is not None:
                doc[key] = val
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc, ensure_ascii=False)


# ── Sentry ────────────────────────────────────────────────────────────────────
def init_sentry(dsn: str, environment: str, release: str | None = None) -> None:
    """Call once at process startup when SENTRY_DSN is set."""
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.redis import RedisIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release,
        integrations=[
            DjangoIntegration(transaction_style="url"),
            CeleryIntegration(monitor_beat_tasks=True),
            RedisIntegration(),
            LoggingIntegration(
                level=logging.INFO,        # breadcrumbs from INFO+
                event_level=logging.ERROR, # Sentry events for ERROR+
            ),
        ],
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.05")),
        send_default_pii=False,
    )
    logging.getLogger("simulator.observability").info(
        "[sentry] initialised env=%s release=%s", environment, release or "unset"
    )


# ── Celery signal hooks ────────────────────────────────────────────────────────
# Imported by trx_simulator/celery.py so they are registered in every worker process.
from celery.signals import task_failure, task_retry  # noqa: E402

_log = logging.getLogger("simulator.observability")
_FAILURES_KEY = "trx:metrics:task_failures"
_MAX_STORED_FAILURES = 100


@task_failure.connect
def on_task_failure(sender, task_id, exception, einfo, **kwargs):
    _log.error(
        "[celery.failure] task=%s id=%s error=%r",
        sender.name, task_id, exception,
    )
    _push_failure_event(sender.name, task_id, repr(exception))


@task_retry.connect
def on_task_retry(sender, task_id, reason, **kwargs):
    _log.warning(
        "[celery.retry] task=%s id=%s reason=%s",
        sender.name, task_id, reason,
    )


def _push_failure_event(task_name: str, task_id: str, error: str) -> None:
    """Push failure event to a Redis ring buffer (last 100 entries)."""
    try:
        from django.conf import settings
        import redis as _redis
        url = getattr(settings, "REDIS_URL", "") or "redis://127.0.0.1:6379/0"
        r = _redis.from_url(url, socket_connect_timeout=1)
        event = json.dumps(
            {"task": task_name, "task_id": task_id, "error": error, "ts": _time.time()},
            ensure_ascii=False,
        )
        pipe = r.pipeline()
        pipe.lpush(_FAILURES_KEY, event)
        pipe.ltrim(_FAILURES_KEY, 0, _MAX_STORED_FAILURES - 1)
        pipe.execute()
    except Exception:
        pass  # observability must never crash the app


# ── WS connection counter helpers ─────────────────────────────────────────────
_WS_KEY = "trx:metrics:ws_connections"


def ws_incr(redis_url: str) -> None:
    """Synchronous increment — call from sync contexts only."""
    try:
        import redis as _redis
        _redis.from_url(redis_url, socket_connect_timeout=1).incr(_WS_KEY)
    except Exception:
        pass


def ws_decr(redis_url: str) -> None:
    """Synchronous decrement clamped at 0."""
    try:
        import redis as _redis
        r = _redis.from_url(redis_url, socket_connect_timeout=1)
        val = r.decr(_WS_KEY)
        if val < 0:
            r.set(_WS_KEY, 0)
    except Exception:
        pass
