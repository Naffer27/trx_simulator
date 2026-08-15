# simulator/ws_events.py
"""
O.6c-1o — single source of truth for publishing "the Position book for
this account changed" to every live WebSocket connection for that
account, via the account_{account_id} Channels group every connection
already joins on connect()/leaves on disconnect() (consumers.py).

Design (O.6c-1n, Option C): this event is NEVER the source of truth.
Its only job is "something changed for this account — go re-read DB".
Every field beyond account_id/action is optional metadata used by the
receiving handler (TradingConsumer.position_changed) for a fast
optimistic patch; the handler always finishes with an authoritative
DB-fresh resync regardless of what this payload said. See that
handler's own docstring for the full contract.

Reused, not reinvented: simulator/tasks.py already had this exact
pattern (async_to_sync(get_channel_layer().group_send) with a
try/except) duplicated twice, for the Celery daemon's two close paths,
before this block. Centralized here so every one of O.6c-1n's 11
Position writers (WS opens/closes/SL/TP/stopout/liquidation, Celery,
Django Admin) calls the same function instead of re-deriving the
try/except/import boilerplate a 9th time.
"""
import logging

log = logging.getLogger("simulator.ws_events")

EVENT_TYPE = "position.changed"

ACTION_OPEN = "open"
ACTION_CLOSE = "close"
ACTION_UPDATE = "update"


def publish_position_changed(account_id, *, action, position_id=None, **extra):
    """
    Fire-and-forget notification to account_{account_id} — Redis-backed
    Channels group in production (channels_redis.core.RedisChannelLayer,
    trx_simulator/settings.py), so this reaches every connection for
    this account regardless of which Daphne worker process holds it.

    Never raises — a publish failure must never affect whether the
    underlying DB write (already committed by the time this runs, see
    each call site's transaction.on_commit/equivalent) succeeded. Same
    fail-open contract as every other observability call in this
    codebase (e.g. broker_audit.record_event()).

    Callable from both sync contexts (tasks.py, consumers.py's
    @database_sync_to_async methods, admin.py's save_model/delete_model
    — all plain sync code) via async_to_sync, matching the exact
    pattern simulator/tasks.py already used for its two pre-existing
    group_send call sites.
    """
    if not account_id:
        return
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        cl = get_channel_layer()
        if not cl:
            return
        async_to_sync(cl.group_send)(
            f"account_{account_id}",
            {"type": EVENT_TYPE, "action": action, "position_id": position_id, **extra},
        )
    except Exception as exc:
        log.warning(
            "[ws_events] publish failed account=%s action=%s position_id=%s: %r",
            account_id, action, position_id, exc,
        )
