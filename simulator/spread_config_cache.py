"""
simulator/spread_config_cache.py — SPREAD-03 FASE A.

Async-safe, process-wide, in-memory cache of BrokerSpreadConfig — the
single source of truth spread_engine.broker_price() and
pricing_context.spread_pips_for()/tick_pricing_snapshot() read from.

Root cause fixed (confirmed in SPREAD-01/SPREAD-02, reproduced with
pre-existing code in test_pricing_context_forensic_invariants.py):
market_data feeds broadcast ticks into TradingConsumer.price_tick(), an
`async def` method, which called spread_engine.broker_price() directly
(synchronously). broker_price()'s old _get_config() did a lazy, per-call
ORM query — a *synchronous* database access performed from *inside* a
running asyncio event loop. Django raises SynchronousOnlyOperation for
that (DJANGO_ALLOW_ASYNC_UNSAFE is not set anywhere in this project, and
this module never sets or reads it), and the old _get_config() swallowed
it in a broad `except Exception`, silently returning None — always. In
practice, BrokerSpreadConfig's base spread had never actually applied via
the live WebSocket path.

Design: reads and writes are strictly separated.
  - get_cached_config(symbol) — the ONLY function broker_price()/
    pricing_context call. Pure in-memory dict lookup. Zero DB. Zero
    exceptions possible. Safe from any context, sync or async, including
    directly inside price_tick().
  - refresh_cache_sync() — the ONLY function allowed to touch the DB.
    Ordinary synchronous ORM code — safe to call directly from a sync
    context (tests, management commands, code already running inside
    database_sync_to_async), and must be wrapped in
    channels.db.database_sync_to_async when called from async code. Never
    called per-tick, never called from inside broker_price().
  - ensure_background_refresh_started() — idempotent; starts the ONE
    process-wide periodic refresh task (via database_sync_to_async, low
    frequency — REFRESH_INTERVAL_SECONDS, matching the previous ad-hoc
    cache's TTL). Called once per TradingConsumer.connect() — cheap no-op
    on every call after the first.

Missing/disabled rows: get_cached_config() returns None (matching the old
_get_config()'s contract exactly) — broker_price() already treats None as
"no base markup, passthrough" (unchanged formula, see spread_engine.py).
refresh_cache_sync() logs a single structured WARNING per refresh cycle
listing every allowed symbol with no enabled config row, so the passthrough
is observable instead of silent — it does not block or change the
fallback itself.

min_spread/max_spread are carried in the cached snapshot ONLY when the
row's spread_bounds_enabled flag is True — otherwise both are surfaced as
None, exactly as if unset. This is the single gating point for the
opt-in floor/ceiling correction made just before SPREAD-04's commit: the
model's min_spread/max_spread had economic defaults (0.50/5.00) that,
once spread_engine/commercial_pricing started reading them, would have
silently narrowed real spreads (e.g. BTCUSD's 15-pip base) for every row
that never had an admin explicitly configure bounds. Gating here means
broker_price()/build_commercial_pricing_profile() need no awareness of
the flag at all — they already treat None as "no clamp".
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("simulator.spread")

REFRESH_INTERVAL_SECONDS = 30.0  # matches the previous per-call cache's TTL


@dataclass(frozen=True)
class SpreadConfigSnapshot:
    symbol: str
    spread_pips: float
    min_spread: Optional[float]
    max_spread: Optional[float]
    bounds_enabled: bool
    enabled: bool


_cache: dict[str, SpreadConfigSnapshot] = {}
_lock = threading.Lock()
_last_refresh_at: Optional[float] = None
_background_task_started = False


def get_cached_config(symbol: str) -> Optional[SpreadConfigSnapshot]:
    """Pure dict read — zero DB, never raises, safe from any context
    including directly inside price_tick(). Returns None if no enabled
    BrokerSpreadConfig row exists for *symbol*, or if the cache has not
    been warmed yet (a brief, expected window right after process start,
    before the first refresh completes)."""
    from market_data.symbol_specs import normalize_symbol
    return _cache.get(normalize_symbol(symbol))


def is_stale(now: Optional[float] = None) -> bool:
    now = now if now is not None else time.monotonic()
    return _last_refresh_at is None or (now - _last_refresh_at) >= REFRESH_INTERVAL_SECONDS


def has_loaded_at_least_once() -> bool:
    return _last_refresh_at is not None


def refresh_cache_sync() -> int:
    """Synchronous DB read — the only function in this module allowed to
    touch the database. Rebuilds the whole cache atomically from every
    enabled BrokerSpreadConfig row. Logs one structured warning per cycle
    for any allowed symbol with no enabled row. Never raises — on any
    failure, logs and leaves the previous cache untouched (a stale-but-
    present cache is safer than wiping it on a transient DB error).

    Returns the number of symbols now cached."""
    global _last_refresh_at
    try:
        from .models import BrokerSpreadConfig
        from market_data.symbol_specs import allowed_symbols

        rows = {
            row.symbol: SpreadConfigSnapshot(
                symbol=row.symbol,
                spread_pips=float(row.spread_pips),
                min_spread=(float(row.min_spread) if (row.spread_bounds_enabled and row.min_spread is not None) else None),
                max_spread=(float(row.max_spread) if (row.spread_bounds_enabled and row.max_spread is not None) else None),
                bounds_enabled=row.spread_bounds_enabled,
                enabled=row.enabled,
            )
            for row in BrokerSpreadConfig.objects.filter(enabled=True)
        }

        with _lock:
            _cache.clear()
            _cache.update(rows)
            _last_refresh_at = time.monotonic()

        missing = sorted(set(allowed_symbols()) - set(rows))
        if missing:
            logger.warning(
                "event=broker_spread_config_missing symbols=%s — passthrough "
                "(no broker markup) applies for these symbols until a "
                "BrokerSpreadConfig row is created",
                missing,
            )
        return len(rows)
    except Exception as exc:
        logger.warning(
            "event=broker_spread_config_refresh_failed error=%r — keeping previous cache", exc,
        )
        return len(_cache)


async def ensure_background_refresh_started(interval_seconds: float = REFRESH_INTERVAL_SECONDS) -> None:
    """Idempotent — starts the one process-wide periodic refresh task, at
    most once, no matter how many WebSocket connections call this (each
    TradingConsumer.connect() calls it; the flag check + set below happens
    with no `await` between them, so it is race-free on a single-threaded
    asyncio event loop). Performs one immediate synchronous-safe refresh
    before returning, so the cache is not empty for the whole first
    interval — safe because this itself runs inside an async function via
    database_sync_to_async, never a raw ORM call in this coroutine."""
    global _background_task_started
    if _background_task_started:
        return
    _background_task_started = True

    from channels.db import database_sync_to_async
    await database_sync_to_async(refresh_cache_sync)()

    async def _loop():
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await database_sync_to_async(refresh_cache_sync)()
            except Exception as exc:
                logger.debug("[spread_config_cache] background refresh failed (non-fatal): %r", exc)

    asyncio.create_task(_loop())


def reset_for_tests() -> None:
    """Test-only — clears the cache and the background-task-started flag
    so each test starts cold. A real running process never needs this."""
    global _last_refresh_at, _background_task_started
    with _lock:
        _cache.clear()
        _last_refresh_at = None
    _background_task_started = False
