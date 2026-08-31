"""
Shared per-process market data feed manager.

One asyncio task per symbol serves ALL connected consumers via Django
Channels group "feed_{symbol}". Each TradingConsumer subscribes on
connect and unsubscribes on disconnect — no per-user upstream connections.

Architecture:
  _feed_loop  → outer restart loop: live → sim (bounded) → live retry
  _try_live   → attempt Binance/Finnhub, returns True if it ran successfully
  _sim_loop   → runs for `duration` seconds then exits (or forever if None)
  _resync_price → HTTP REST fetch to snap sim price to real market
"""

import asyncio
import json
import logging
import math
import os
import random
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional

try:
    import websockets
except ImportError:
    websockets = None

log = logging.getLogger("simulator.ws")

FINNHUB_API_KEY      = (os.getenv("FINNHUB_API_KEY", "") or "").strip()
DEFAULT_TICK_INTERVAL = float(os.getenv("PRICE_TICK_INTERVAL", "1.0"))

# How long (seconds) to stay in sim before retrying the live feed
SIM_LIVE_RETRY_SECS  = int(os.getenv("SIM_LIVE_RETRY_SECS",  "60"))
# How often (seconds) to resync sim price to real market while in sim mode
SIM_RESYNC_INTERVAL  = int(os.getenv("SIM_RESYNC_INTERVAL",  "30"))

# O.6c-1v — how often the position-backed feed keepalive re-derives, from
# Position (the DB authority), which symbols must have a running feed task
# regardless of chart-subscriber count. Kept comfortably under
# _PRICE_CACHE_TTL (60s, below) so a symbol whose only chart subscriber
# just disconnected never has its Redis price go stale before this catches
# it and keeps the feed task alive.
POSITION_FEED_RECONCILE_INTERVAL_SECONDS = float(
    os.getenv("POSITION_FEED_RECONCILE_INTERVAL", "15")
)


# ─── symbol helpers ────────────────────────────────────────────────────────────
# Thin wrappers — all instrument parameters sourced from the symbol registry.

from market_data.symbol_specs import get_spec as _get_spec


def _step_dec(symbol: str) -> tuple[float, int]:
    sp = _get_spec(symbol)
    return (sp.tick_size, sp.price_decimals)

def _spread(symbol: str) -> float:
    return _get_spec(symbol).spread

def _drift(symbol: str) -> float:
    return _get_spec(symbol).sim_drift

def _fallback_price(symbol: str) -> float:
    """Last-resort price — only used if live feed and REST resync both fail."""
    return _get_spec(symbol).base_price

def _binance_sym(symbol: str) -> str | None:
    return _get_spec(symbol).exchange_symbol

def _kraken_sym(symbol: str) -> str | None:
    return _get_spec(symbol).kraken_symbol

# GOLDEN-SCENARIOS-MARKETDATA-01 — Kraken exposes each pair under TWO
# different names depending on endpoint: the WebSocket API takes the
# "wsname" (e.g. "XBT/USD", with a slash — this is exactly what
# SymbolSpec.kraken_symbol/_kraken_sym() already holds, and what
# _kraken_loop()'s live-tick WS subscription correctly uses, verified
# live against wss://ws.kraken.com). The REST /0/public/OHLC endpoint
# instead requires the "altname" (e.g. "XBTUSD", no slash) — passing the
# wsname there returns HTTP 200 with {"error":["EQuery:Unknown asset
# pair"]}, verified live. Deliberately a small, explicit, per-symbol
# dict — not a blind "strip the slash" transform — even though that
# transform happens to hold for both entries today (verified live via
# Kraken's own /0/public/AssetPairs for both XBT/USD and ETH/USD): a
# silent transform would fail differently (and less visibly) for a
# future Kraken-mapped symbol whose altname doesn't follow that pattern.
# symbol_specs.py::kraken_symbol is NOT changed — it stays correct for
# the WS path, which is untouched by this dict.
_KRAKEN_REST_PAIR = {
    "BTCUSD": "XBTUSD",
    "ETHUSD": "ETHUSD",
}

def _kraken_rest_pair(symbol: str) -> str | None:
    return _KRAKEN_REST_PAIR.get(symbol)

def _finnhub_sym(symbol: str) -> str:
    """Finnhub symbol string — use spec value when available."""
    sp = _get_spec(symbol)
    if sp.finnhub_symbol:
        return sp.finnhub_symbol
    s = symbol.upper()
    if "/" in s:
        a, b = s.split("/", 1)
        return f"FX:{a}{b}"
    return s


# ─── FIX-05B.1 — Closed-Candle Filter ───────────────────────────────────────────
#
# Pure, provider-agnostic helpers: given a bar's OPEN time (seconds) and a
# timeframe's duration (seconds), is that bar actually closed yet? Verified
# live against both Binance (open-time, ms) and Kraken (open-time, s) — both
# return the still-forming bar as the last row of a kline/OHLC response
# (FIX-05B.1 design lock §B). Deliberately takes tf_seconds as an int, not a
# timeframe string: market_data/feeds.py must not import simulator/
# consumers.py::tf_seconds() (circular import — consumers.py already imports
# FROM this module) and must not duplicate that mapping either. The one
# caller with tf_seconds() already in scope (generate_history(), consumers.py)
# passes the resolved seconds in — this stays the single authoritative
# closed-candle filter point, applied once, not duplicated at the fetch site.
def _is_closed(candle_open_time_sec: int, tf_seconds_value: int, now_sec: int) -> bool:
    return candle_open_time_sec + tf_seconds_value <= now_sec


def _closed_only(bars: list, tf_seconds_value: int, now_sec: "int | None" = None) -> list:
    """Filter, not "drop the last element" — correct even if a provider ever
    returns more than one still-forming bar, or bars out of order."""
    if now_sec is None:
        now_sec = int(time.time())
    return [b for b in bars if _is_closed(b["time"], tf_seconds_value, now_sec)]


# ─── O.6c-1w — Price Integrity / Plausibility Gate ──────────────────────────────
#
# get_validated_quote() (FeedManager method, below) is the single
# authoritative point deciding whether a symbol's current quote may be
# used for ANY financial decision: P&L, equity, margin, manual close,
# SL/TP, stopout, retail liquidation. Returns None on ANY failure —
# absence, staleness, structural corruption, or implausible magnitude
# (Capa A) — NEVER a synthetic/fabricated substitute. Generalizes the
# "exclude, never fabricate" policy already established by O.6c-1q/
# O.6c-1s/broker_exposure.py FASE-4 to one choke point, closing the
# exact O.6c-1t gap: has_price() and last_bid()/last_ask() were 2+
# independent lock acquisitions, never guaranteed to observe the same
# instant — a symbol could pass has_price()==True while last_bid()/
# last_ask() returned a value of a completely different instrument's
# magnitude (the ~$63,087,429.83 EUR/USD incident).
#
# Capa A (approved, O.6c-1w decision): ±1 order of magnitude vs
# SymbolSpec.base_price. Capa B (tick-to-tick deviation vs
# last_valid_quote) is explicitly NOT active yet — the architecture
# (_last_valid_quote, updated on every successful validation) is
# prepared below, but nothing reads it back for a rejection decision.
# That threshold is a business/risk decision not yet made.

@dataclass(frozen=True)
class Quote:
    """An atomic, internally-coherent price snapshot for one symbol —
    the ONLY shape get_validated_quote() ever returns. mid is always
    derived from bid/ask by the validator, never trusted from a
    separately-written field, so bid/ask/mid coherence holds by
    construction rather than by convention."""
    symbol: str
    bid: float
    ask: float
    mid: float
    timestamp: float
    source: str  # "binance" | "kraken" | "finnhub" | "sim" | "rest_resync" | "unknown"


def _validate_quote_values(symbol: str, bid, ask) -> bool:
    """O.6c-1w — pure structural + Capa A plausibility check, no
    FeedManager instance state needed (only the read-only symbol
    registry). Shared by FeedManager.get_validated_quote() (in-process,
    Daphne) and tasks.py's _read_cached_price() (Celery, a SEPARATE
    process reading the same values via Redis) — the one place either
    of them decides "is this bid/ask usable", so the two can never
    silently drift apart into different definitions of valid.

    Structural (sections 3 of the O.6c-1w design — no business number
    involved): both finite (rejects NaN/±Infinity), both > 0, ask >= bid.

    Capa A (section 4, O.6c-1w-approved): the candidate must be within
    one order of magnitude of SymbolSpec.base_price for *symbol* — the
    exact, reproducible check that would have rejected the ~63088
    BTCUSD-magnitude value observed for EUR/USD in O.6c-1t
    (log10(63088/1.17) ≈ 4.7, far outside ±1)."""
    if bid is None or ask is None:
        return False
    if not (isinstance(bid, (int, float)) and isinstance(ask, (int, float))):
        return False
    if isinstance(bid, bool) or isinstance(ask, bool):
        return False  # bool is a subclass of int — never a real quote value
    if not (math.isfinite(bid) and math.isfinite(ask)):
        return False
    if bid <= 0 or ask <= 0:
        return False
    if ask < bid:
        return False

    mid = (bid + ask) / 2.0
    try:
        base = _fallback_price(symbol)
    except KeyError:
        return False  # unknown symbol — never guess a plausibility band
    if not (math.isfinite(base) and base > 0):
        return False
    try:
        if abs(math.log10(mid / base)) > 1.0:
            return False
    except ValueError:
        return False  # defensive only — mid<=0 already rejected above

    return True


# ─── Redis price cache (cross-process, for daemon/Celery access) ───────────────
# Key schema: trx:price:bid:{symbol}, trx:price:ask:{symbol},
# trx:price:source:{symbol} (FIX-05A.1 — same TTL, same pipeline, so the
# three keys always expire/refresh together; the daemon's _read_cached_price()
# fail-closes on a missing or "sim" source, see simulator/tasks.py).
# TTL: 60 s — if the feed stops, keys expire and the daemon skips those positions.
# Failures are silent: a Redis outage must never bring down the feed loop.

_PRICE_CACHE_TTL  = int(os.getenv("PRICE_CACHE_TTL", "60"))
_PRICE_CACHE_KEY_PREFIX = "trx:price"

# Shared thread pool for fire-and-forget Redis writes (1 thread is enough).
_redis_write_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="price_cache")


def _write_price_cache_sync(symbol: str, bid: float, ask: float, source: str) -> None:
    """Write bid/ask/source to Redis. Called from a thread pool — must never raise."""
    try:
        from django.conf import settings as _s
        import redis as _redis
        url = getattr(_s, "REDIS_URL", "").strip() or "redis://127.0.0.1:6379/0"
        r = _redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
        pipe = r.pipeline(transaction=False)
        pipe.setex(f"{_PRICE_CACHE_KEY_PREFIX}:bid:{symbol}", _PRICE_CACHE_TTL, str(bid))
        pipe.setex(f"{_PRICE_CACHE_KEY_PREFIX}:ask:{symbol}", _PRICE_CACHE_TTL, str(ask))
        pipe.setex(f"{_PRICE_CACHE_KEY_PREFIX}:source:{symbol}", _PRICE_CACHE_TTL, str(source))
        pipe.execute()
    except Exception as exc:
        # Intentionally swallowed — Redis down must not crash the feed loop.
        log.debug("[price_cache] write failed for %s: %r", symbol, exc)


async def _write_price_cache(symbol: str, bid: float, ask: float, source: str) -> None:
    """Non-blocking wrapper: dispatches the Redis write to the thread pool."""
    loop = asyncio.get_event_loop()
    loop.run_in_executor(_redis_write_pool, _write_price_cache_sync, symbol, bid, ask, source)


# ─── singleton ─────────────────────────────────────────────────────────────────

_MANAGER = None

def get_feed_manager() -> "FeedManager":
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = FeedManager()
    return _MANAGER


# ─── FeedManager ───────────────────────────────────────────────────────────────

class FeedManager:
    """
    Manages one upstream feed task per symbol.
    Broadcasts price.tick events to Channels group "feed_{safe_sym}".
    """

    def __init__(self):
        self._tasks:  dict[str, asyncio.Task] = {}
        self._counts: dict[str, int]          = {}
        self._prices: dict[str, float]        = {}
        self._bids:   dict[str, float]        = {}
        self._asks:   dict[str, float]        = {}
        # PANEL-02 INVARIANTE-1 — wall-clock time of the last REAL price
        # update per symbol, used by has_price()'s freshness check. Set
        # ONLY on a genuine live/sim tick (_broadcast) or a successful REST
        # resync (_resync_price's success branch) — deliberately NEVER set
        # by _resync_price's fallback branch (a static, non-live price) or
        # by any other code path, so a fallback-seeded _prices entry is
        # always correctly treated as stale/absent by has_price(), never
        # as fresh.
        self._price_ts: dict[str, float] = {}
        # O.6c-1v — OPEN POSITION FEED COVERAGE. Symbols with at least one
        # currently-open Position, as last derived from the DB (the sole
        # authority — see sync_position_symbol_from_db()/mark_position_symbol()/
        # _position_feed_reconcile_once() below). A symbol in this set keeps
        # its feed task alive even with zero chart subscribers — see
        # unsubscribe()'s guard. Guarded by self._lock (below), same as the
        # four price dicts, since it's read/written from both the asyncio
        # event loop and database_sync_to_async threadpool threads.
        self._position_symbols: set[str] = set()
        # O.6c-1v — guards ensure_position_feed_reconciliation_started()
        # against starting its background loop more than once per process,
        # same idempotency contract as spread_config_cache's
        # _background_task_started (SPREAD-03) — reused pattern, not a new one.
        self._position_reconcile_started: bool = False
        # O.6c-1ac — the actual asyncio.Task running the periodic
        # reconciliation loop, once created. Exists specifically so
        # _position_reconcile_started's truthfulness can be verified
        # (flag True must always imply this is not None) instead of the
        # flag alone being trusted — see ensure_position_feed_
        # reconciliation_started()'s try/finally below and
        # is_position_reconciliation_alive().
        self._position_reconcile_task: "asyncio.Task | None" = None
        # O.6c-1w — which provider produced the current _prices/_bids/
        # _asks entry for each symbol ("binance"/"kraken"/"finnhub"/
        # "sim"/"rest_resync") — pure traceability metadata returned on
        # Quote.source, never itself a validity criterion. Written
        # alongside the four price dicts (same lock, same call), read
        # together with them in get_validated_quote()'s single atomic
        # snapshot.
        self._price_source: dict[str, str] = {}
        # O.6c-1w — updated ONLY inside get_validated_quote(), only on
        # the success path (a quote that passed every structural +
        # Capa A check). Architecture prepared for Capa B (tick-to-tick
        # deviation vs. this value) — O.6c-1w's approved decision is
        # that NOTHING reads this back for a rejection decision yet;
        # that threshold is a business/risk decision not yet made. See
        # last_valid_quote() below for the (currently informational
        # only) accessor.
        self._last_valid_quote: dict[str, "Quote"] = {}
        # PANEL-02 — guards all reads/writes of the four dicts above. This
        # class's own feed tasks (_broadcast/_resync_price) run on the
        # asyncio event loop thread, but has_price()/last_price()/
        # last_bid()/last_ask() are also called synchronously from
        # database_sync_to_async threadpool threads (the atomic order
        # guard in consumers.py). Individual dict get/set calls are
        # GIL-atomic in CPython, but a multi-field update (price+bid+ask+
        # timestamp together in _broadcast) is not — without this lock a
        # reader could observe a torn update (e.g. a fresh timestamp with
        # a stale bid/ask, or vice versa).
        self._lock = threading.Lock()

    # ── public API ──

    @staticmethod
    def group_for(symbol: str) -> str:
        return "feed_" + symbol.replace("/", "_")

    def last_price(self, symbol: str) -> float:
        with self._lock:
            return self._prices.get(symbol, _fallback_price(symbol))

    def has_price(self, symbol: str, max_age_seconds: float = _PRICE_CACHE_TTL) -> bool:
        """PANEL-02 — True only if *symbol* has a REAL, live/cached bid+ask
        in this process's shared feed cache AND it was last updated within
        *max_age_seconds* (default: the same TTL the Redis cross-process
        price cache uses, _PRICE_CACHE_TTL — reused rather than inventing a
        second staleness threshold).

        Presence alone is NOT sufficient: a stalled feed (no ticks for
        minutes — provider down, symbol simply idle) leaves old values
        sitting in _prices/_bids/_asks indefinitely; trusting those as
        "the current price" for a financial risk decision (fresh_equity in
        the atomic order guard) would silently understate a real, ongoing
        loss. A fallback-seeded entry (_resync_price's except branch,
        REST resync failed with nothing prior) is likewise never fresh —
        it never receives a _price_ts entry, so it fails this check the
        same way a stale one does. Used by the atomic order-open guard to
        decide whether a position's floating PnL can be safely computed —
        if not, the whole order is rejected (never a fabricated/zeroed
        PnL) — see _db_open_position_atomic.
        """
        with self._lock:
            if symbol not in self._prices:
                return False
            ts = self._price_ts.get(symbol)
            if ts is None:
                return False
            return (time.time() - ts) <= max_age_seconds

    def last_bid(self, symbol: str) -> float:
        with self._lock:
            return self._bids.get(symbol, _fallback_price(symbol) - _spread(symbol) / 2)

    def last_ask(self, symbol: str) -> float:
        with self._lock:
            return self._asks.get(symbol, _fallback_price(symbol) + _spread(symbol) / 2)

    # ── O.6c-1w — Price Integrity / Plausibility Gate ──

    def get_validated_quote(self, symbol: str, max_age_seconds: float = _PRICE_CACHE_TTL) -> "Quote | None":
        """O.6c-1w — the single authoritative point deciding whether
        *symbol*'s current quote may be used for ANY financial
        decision: P&L, equity, margin, manual close, SL/TP, stopout,
        retail liquidation. Returns None on ANY failure — absence,
        staleness, structural corruption, or implausible magnitude
        (Capa A) — NEVER has_price()/last_bid()/last_ask()'s synthetic
        fallback. See the module-level docstring above _validate_quote_
        values() for the full rationale and the exact O.6c-1t incident
        this closes.

        Reads timestamp + bid + ask + source as ONE snapshot under
        self._lock — the atomicity has_price()+last_bid()+last_ask()
        (3 separate lock acquisitions) never had. mid is derived from
        bid/ask here, never read from self._prices — bid/ask/mid
        coherence holds by construction.

        On success, records the Quote into self._last_valid_quote —
        Capa B architecture, not yet read back for any rejection
        decision (O.6c-1w's explicit, approved scope)."""
        with self._lock:
            ts = self._price_ts.get(symbol)
            if ts is None:
                return None
            if (time.time() - ts) > max_age_seconds:
                return None
            bid = self._bids.get(symbol)
            ask = self._asks.get(symbol)
            source = self._price_source.get(symbol, "unknown")

        if not _validate_quote_values(symbol, bid, ask):
            return None

        quote = Quote(
            symbol=symbol, bid=bid, ask=ask, mid=(bid + ask) / 2.0,
            timestamp=ts, source=source,
        )
        with self._lock:
            self._last_valid_quote[symbol] = quote
        return quote

    def last_valid_quote(self, symbol: str) -> "Quote | None":
        """O.6c-1w — the most recent Quote that passed
        get_validated_quote()'s full validation for *symbol*, if any.
        Informational only today (Capa B is not active) — nothing in
        this codebase currently reads this back for a rejection
        decision; exposed as its own accessor so that future work (and
        tests) can inspect it without reaching into a private dict."""
        with self._lock:
            return self._last_valid_quote.get(symbol)

    # ── O.6c-1v — open position feed coverage ──
    #
    # A symbol's feed task must stay alive while EITHER of two independent
    # reference kinds is non-zero: chart_subscribers (self._counts, the
    # pre-existing mechanism above) OR open_position_references
    # (self._position_symbols, below). Position is the DB authority for
    # the second kind — never a connection-scoped counter, since a
    # Position write can commit from three different processes (this
    # Daphne worker's own WS consumer, Django Admin in the same process,
    # or the Celery daemon in a SEPARATE process that never shares this
    # FeedManager instance's memory at all). See
    # docs — O.6c-1u section 9 already established Celery cannot reach
    # this singleton directly; _position_feed_reconcile_once() below is
    # what makes Celery-driven closes (and a post-restart Daphne process
    # that has open Positions from before it existed) correctly reflected
    # here, without depending on any single writer's event firing.

    def has_position_ref(self, symbol: str) -> bool:
        """True if *symbol* was last known (via mark/unmark/sync/reconcile
        below) to have at least one open Position — independent of chart
        subscriber count."""
        with self._lock:
            return symbol in self._position_symbols

    def mark_position_symbol(self, symbol: str) -> None:
        """Record that *symbol* has (at least) one open Position. Sync,
        thread-safe (self._lock) — callable directly from a
        database_sync_to_async transaction body (consumers.py) or a plain
        sync Django Admin method, no event loop required. Idempotent —
        safe to call on every open/merge, never needs a matching count."""
        with self._lock:
            self._position_symbols.add(symbol)

    def unmark_position_symbol(self, symbol: str) -> None:
        """Inverse of mark_position_symbol() — used only by the
        reconciliation loop below, which has already confirmed via a
        fresh DB read that no open Position remains for *symbol*."""
        with self._lock:
            self._position_symbols.discard(symbol)

    def sync_position_symbol_from_db(self, symbol: str) -> None:
        """O.6c-1v — call right after a Position close/delete for
        *symbol* commits (same process: consumers.py's
        _db_close_position_atomic, admin.py's PositionAdmin.delete_model).
        Re-derives presence from Position.objects.filter(symbol=...).exists()
        rather than decrementing a counter — deliberately immune to
        double-decrement/missed-decrement bugs across the many close paths
        (manual, Close All, SL, TP, stopout, retail liquidation) that all
        converge on _db_close_position_atomic, and correctly keeps the
        symbol marked when a SECOND open position on the same symbol is
        still open (test scenario #5)."""
        from simulator.models import Position
        still_open = Position.objects.filter(symbol=symbol).exists()
        with self._lock:
            if still_open:
                self._position_symbols.add(symbol)
            else:
                self._position_symbols.discard(symbol)

    async def sync_position_symbol_from_db_async(self, symbol: str) -> None:
        """O.6c-1ac — async wrapper around sync_position_symbol_from_db()
        for call sites that run on the asyncio event loop and cannot call
        the ORM directly (unsubscribe()'s race guard, below). Same
        DB-authoritative re-derivation, same query, no new logic — just a
        safe way to reach it from an async method."""
        from channels.db import database_sync_to_async
        await database_sync_to_async(self.sync_position_symbol_from_db)(symbol)

    async def _position_feed_reconcile_once(self) -> None:
        """O.6c-1v — the DB-authoritative backstop. Re-reads the full set
        of symbols with an open Position and reconciles self._position_symbols
        (and, for any newly-discovered symbol, starts its feed task) against
        it. This is what covers: (a) Celery-driven closes — a different
        process, can never call mark/unmark/sync above directly; (b) a
        freshly-restarted Daphne process that has pre-existing open
        Positions from before it started — the first call after restart
        naturally discovers them, since self._position_symbols starts
        empty; (c) a self-healing backstop if any same-process call site
        above were ever missed. Runs on its own periodic timer
        (POSITION_FEED_RECONCILE_INTERVAL_SECONDS), entirely decoupled
        from the tick loop — never an added query per market tick."""
        from channels.db import database_sync_to_async
        from channels.layers import get_channel_layer
        from simulator.models import Position

        def _open_symbols() -> set[str]:
            return set(Position.objects.values_list("symbol", flat=True).distinct())

        db_symbols = await database_sync_to_async(_open_symbols)()
        with self._lock:
            previously_marked = set(self._position_symbols)
        to_add    = db_symbols - previously_marked
        to_remove = previously_marked - db_symbols
        if not to_add and not to_remove:
            return

        channel_layer = get_channel_layer()
        for sym in sorted(to_add):
            self.mark_position_symbol(sym)
            if channel_layer is not None:
                self._ensure_running(sym, channel_layer)
            log.info("[feed] position-backed keepalive started for %s (reconciliation)", sym)
        for sym in sorted(to_remove):
            self.unmark_position_symbol(sym)
            if self._counts.get(sym, 0) <= 0:
                self._stop(sym)
                log.info(
                    "[feed] position-backed keepalive released for %s "
                    "(no open positions, no chart subscribers)", sym,
                )

    async def ensure_position_feed_reconciliation_started(self) -> None:
        """O.6c-1v — idempotent; starts the ONE process-wide periodic
        reconciliation task, at most once, no matter how many WebSocket
        connections call this (mirrors spread_config_cache's
        ensure_background_refresh_started() / SPREAD-03 exactly — reused
        pattern, not reinvented: flag check + set with no `await` between
        them, race-free on a single-threaded asyncio event loop). Performs
        one immediate pass before returning, so a symbol with a
        pre-existing open Position and no chart subscriber gets its feed
        task back within this call, not after a full interval.

        O.6c-1ac — FAIL-RECOVERABLE LIFECYCLE. O.6c-1ab found that the
        original version awaited the first reconcile pass with no
        try/except AFTER already setting the idempotency flag: any
        exception (a real DB error, or even asyncio.CancelledError from
        a connect() that got interrupted mid-handshake) skipped the
        `asyncio.create_task(_loop())` line entirely, leaving the flag
        permanently True with no retry loop ever created — reconciliation
        dead for the rest of the process's life, recoverable only by a
        full restart. The first pass is now wrapped in try/finally: the
        loop task is created in the `finally` clause, so it always gets
        created exactly once, whether the first pass succeeded, raised a
        business exception, or was cancelled. self._position_reconcile_
        task holds the real task object so the flag's truthfulness
        ("a retry loop exists") can be verified directly instead of
        trusted blindly — see is_position_reconciliation_alive()."""
        if self._position_reconcile_started:
            return
        self._position_reconcile_started = True
        try:
            await self._position_feed_reconcile_once()
        except Exception as exc:
            log.error(
                "[feed] initial position reconcile failed (non-fatal) — "
                "the periodic retry loop will still start: %r", exc,
            )
        finally:
            async def _loop():
                while True:
                    await asyncio.sleep(POSITION_FEED_RECONCILE_INTERVAL_SECONDS)
                    try:
                        await self._position_feed_reconcile_once()
                    except Exception as exc:
                        log.error("[feed] position reconcile failed (non-fatal): %r", exc)

            self._position_reconcile_task = asyncio.create_task(_loop())

    def is_position_reconciliation_alive(self) -> bool:
        """O.6c-1ac — True only if the periodic reconciliation loop task
        genuinely exists and has not finished/crashed. Lets callers
        (tests, diagnostics) verify _position_reconcile_started's claim
        instead of trusting the flag alone — the exact gap O.6c-1ab's
        audit found: a flag that could be True with no loop behind it."""
        task = self._position_reconcile_task
        return task is not None and not task.done()

    def reset_position_tracking_for_tests(self) -> None:
        """Test-only — mirrors spread_config_cache.reset_for_tests()."""
        with self._lock:
            self._position_symbols.clear()
        self._position_reconcile_started = False
        task = self._position_reconcile_task
        if task is not None and not task.done():
            try:
                task.cancel()
            except Exception:
                pass
        self._position_reconcile_task = None

    async def subscribe(self, symbol: str, channel_layer, channel_name: str) -> None:
        await channel_layer.group_add(self.group_for(symbol), channel_name)
        self._counts[symbol] = self._counts.get(symbol, 0) + 1
        self._ensure_running(symbol, channel_layer)

    async def unsubscribe(self, symbol: str, channel_layer, channel_name: str) -> None:
        await channel_layer.group_discard(self.group_for(symbol), channel_name)
        count = max(0, self._counts.get(symbol, 1) - 1)
        self._counts[symbol] = count
        # O.6c-1v — a symbol with an open Position keeps its feed task
        # alive even at zero chart subscribers; only actually stop when
        # BOTH reference kinds are empty. has_position_ref() reflects the
        # last DB-derived state (kept current by mark_position_symbol/
        # sync_position_symbol_from_db at every same-process open/close,
        # and self-healed by the periodic reconciliation backstop) — never
        # a fresh query on this hot path.
        if count <= 0 and not self.has_position_ref(symbol):
            # O.6c-1ac — RACE GUARD. has_position_ref() reflects the last
            # DB-derived state, but that state can genuinely lag a real
            # open Position during bootstrap or a concurrent connect():
            # ensure_position_feed_reconciliation_started() sets its
            # idempotency flag synchronously and then awaits a DB hop —
            # a second, concurrent connect() (default symbol subscribe
            # immediately followed by change_symbol/unsubscribe, exactly
            # O.6c-1ab's documented race) can see the flag already set,
            # skip its own reconciliation, and reach this point before
            # the FIRST caller's DB query has returned and marked
            # _position_symbols. This is the only place in the hot tick
            # path a DB read is added — and only right here, only once,
            # only when a symbol's LAST remaining reference is about to
            # be released and the cache doesn't already know about a
            # Position. Position (DB) stays the sole authority; no
            # manual counter is introduced.
            await self.sync_position_symbol_from_db_async(symbol)
            if self._counts.get(symbol, 0) <= 0 and not self.has_position_ref(symbol):
                self._stop(symbol)

    # ── internal ──

    def _ensure_running(self, symbol: str, channel_layer) -> None:
        task = self._tasks.get(symbol)
        if task is None or task.done():
            self._maybe_run_shadow_evaluation(symbol)
            self._tasks[symbol] = asyncio.create_task(
                self._feed_loop(symbol, channel_layer),
                name=f"feed_{symbol}",
            )
            log.info("[feed] started task for %s", symbol)

    def _maybe_run_shadow_evaluation(self, symbol: str) -> None:
        """
        FOUNDATION-08 — observational only, gated by settings.MARKET_DATA_
        SHADOW_MODE (default False). Runs at most once per cold start of a
        symbol's feed task (same guard as the real task creation just below,
        never per-tick). Any failure here is swallowed — the real feed task
        is created unconditionally right after this returns, whether it
        succeeded, was skipped, or raised. See market_data/shadow/.
        """
        try:
            from django.conf import settings
            if not getattr(settings, "MARKET_DATA_SHADOW_MODE", False):
                return
            from market_data.shadow.service import evaluate_shadow_route
            result = evaluate_shadow_route(symbol)
            log.info(
                "event=market_data_shadow_decision symbol=%s legacy_provider=%s "
                "shadow_provider=%s agrees=%s reason_code=%s degraded=%s error_code=%s",
                symbol, result.legacy_expected_provider, result.shadow_selected_provider,
                result.agrees_with_legacy,
                result.reason_code.value if result.reason_code else None,
                result.degraded, result.error_code,
            )
        except Exception as exc:
            log.debug("[shadow] evaluation failed for %s (non-fatal, legacy unaffected): %r", symbol, exc)

    def _stop(self, symbol: str) -> None:
        task = self._tasks.pop(symbol, None)
        if task and not task.done():
            task.cancel()
            log.info("[feed] stopped task for %s (no subscribers)", symbol)
        with self._lock:
            self._prices.pop(symbol, None)
            self._bids.pop(symbol, None)
            self._asks.pop(symbol, None)
            self._price_ts.pop(symbol, None)
            # O.6c-1w — reset alongside the four price dicts above, same
            # reasoning: a stopped feed's stale metadata/last-valid-quote
            # must never be mistaken for current state if the symbol's
            # feed task restarts later.
            self._price_source.pop(symbol, None)
            self._last_valid_quote.pop(symbol, None)
        self._counts.pop(symbol, None)

    async def _broadcast_kline(self, symbol: str, cl, bar: dict) -> None:
        """Broadcast a canonical exchange candle to all consumers subscribed to this symbol."""
        await cl.group_send(
            self.group_for(symbol),
            {"type": "candle.kline", "symbol": symbol, "data": bar},
        )

    async def fetch_kline_history(
        self, symbol: str, interval: str = "1m", limit: int = 200
    ) -> list:
        """
        Fetch historical klines for *symbol*. Returns bars oldest→newest.
        Sources tried in order: Binance US → Binance com → Kraken.
        """
        mapped = _binance_sym(symbol)
        if not mapped:
            return []

        loop = asyncio.get_event_loop()

        def _fetch(url: str) -> bytes:
            req = urllib.request.Request(url, headers={"User-Agent": "trx-sim/1.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.read()

        def _parse_binance(raw: bytes) -> list:
            bars = []
            for row in json.loads(raw):
                bars.append({
                    "time":   int(row[0]) // 1000,  # ms → seconds
                    "open":   float(row[1]),
                    "high":   float(row[2]),
                    "low":    float(row[3]),
                    "close":  float(row[4]),
                    "volume": float(row[5]),
                })
            return bars

        # ── 1. Binance US (no geo-block for US/LATAM regions) ──
        try:
            raw = await loop.run_in_executor(
                None, _fetch,
                f"https://api.binance.us/api/v3/klines?symbol={mapped}&interval={interval}&limit={limit}",
            )
            bars = _parse_binance(raw)
            log.info("[feed] Binance US klines %s %s — %d bars", symbol, interval, len(bars))
            return bars
        except Exception as exc:
            log.debug("[feed] Binance US klines unavailable for %s: %r", symbol, exc)

        # ── 2. Binance com ──
        try:
            raw = await loop.run_in_executor(
                None, _fetch,
                f"https://api.binance.com/api/v3/klines?symbol={mapped}&interval={interval}&limit={limit}",
            )
            bars = _parse_binance(raw)
            log.info("[feed] Binance com klines %s %s — %d bars", symbol, interval, len(bars))
            return bars
        except Exception as exc:
            log.debug("[feed] Binance com klines unavailable for %s: %r", symbol, exc)

        # ── 3. Kraken OHLC fallback ──
        # GOLDEN-SCENARIOS-MARKETDATA-01 — REST needs the altname
        # (_kraken_rest_pair, e.g. "XBTUSD"), never the wsname _kraken_sym()
        # returns (e.g. "XBT/USD") — that's _kraken_loop()'s (WS, live
        # ticks) pair, untouched here. Using the wsname against this REST
        # endpoint returns HTTP 200 with {"error":["EQuery:Unknown asset
        # pair"]} — silently swallowed by the except below before this fix,
        # never a real bar.
        _KR_INTERVAL = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "1d": 1440}
        kr_rest_pair = _kraken_rest_pair(symbol)
        kr_intv      = _KR_INTERVAL.get(interval)
        if kr_rest_pair and kr_intv:
            try:
                raw  = await loop.run_in_executor(
                    None, _fetch,
                    f"https://api.kraken.com/0/public/OHLC?pair={kr_rest_pair}&interval={kr_intv}",
                )
                data = json.loads(raw)
                rows = list(data["result"].values())[0]  # first key is the pair data
                # Kraken row: [time, open, high, low, close, vwap, volume, count]
                bars = [
                    {
                        "time":   int(row[0]),
                        "open":   float(row[1]),
                        "high":   float(row[2]),
                        "low":    float(row[3]),
                        "close":  float(row[4]),
                        "volume": float(row[6]),
                    }
                    for row in rows[-limit:]
                ]
                log.info("[feed] Kraken klines %s %s — %d bars", symbol, interval, len(bars))
                return bars
            except Exception as exc:
                # GOLDEN-SCENARIOS-MARKETDATA-01 — was log.debug (invisible
                # at normal levels), which is exactly why this pair-mapping
                # bug went unnoticed. exc's repr is a KeyError/ValueError/
                # URLError message — never response bodies or credentials
                # (this endpoint is unauthenticated) — safe to log at
                # warning.
                log.warning("[feed] Kraken klines unavailable for %s %s: %r", symbol, interval, exc)

        log.error("[feed] All kline history sources failed for %s %s", symbol, interval)
        return []

    async def _broadcast(self, symbol: str, cl, bid: float, ask: float, ts: int,
                          source: str = "live") -> None:
        _, dec = _step_dec(symbol)
        mid = round((bid + ask) / 2, dec)
        with self._lock:
            self._bids[symbol]         = bid
            self._asks[symbol]         = ask
            self._prices[symbol]       = mid
            self._price_ts[symbol]     = time.time()
            self._price_source[symbol] = source  # O.6c-1w — Quote.source metadata
        # Write to Redis so cross-process readers (Celery daemon) can access prices.
        await _write_price_cache(symbol, bid, ask, source)
        # FOUNDATION-13 — records only a timestamp (no bid/ask/mid) in the
        # observability store, gated by MARKET_DATA_OBSERVABILITY_ENABLED.
        if self._observability_enabled():
            try:
                from market_data.observability import record_tick
                record_tick(symbol)
            except Exception as exc:
                log.debug("[observability] tick recording failed for %s (non-fatal): %r", symbol, exc)
        await cl.group_send(
            self.group_for(symbol),
            {
                "type":   "price.tick",
                "symbol": symbol,
                "bid":    bid,
                "ask":    ask,
                "mid":    mid,
                "time":   ts,
                # FIX-05A.1 — aditivo, no rompe ningún reader existente
                # (price_tick() es el único, accede por clave explícita).
                # Autoridad financiera de SL/TP en vivo vs display; ver
                # consumers.py::price_tick().
                "source": source,
            },
        )

    # ── outer restart loop ──

    async def _feed_loop(self, symbol: str, channel_layer) -> None:
        """
        Outer loop: attempt live feed → if it fails, run sim for SIM_LIVE_RETRY_SECS
        seconds (with periodic REST resyncs), then retry live.
        Never permanently stuck in sim.
        """
        # Seed the price from real market before the first tick
        await self._resync_price(symbol)

        while True:
            # 1. Try live feed (Binance then Finnhub)
            live_ran = await self._try_live(symbol, channel_layer)

            if live_ran:
                # Live feed exited cleanly (shouldn't normally happen unless cancelled)
                continue

            # 2. Live unavailable → bounded sim with periodic resync
            log.info(
                "[feed] %s entering sim for %ds, will retry live after",
                symbol, SIM_LIVE_RETRY_SECS,
            )
            await self._resync_price(symbol)
            await self._sim_loop(symbol, channel_layer, duration=SIM_LIVE_RETRY_SECS)
            log.info("[feed] %s sim period done — retrying live feed", symbol)

    async def _try_live(self, symbol: str, channel_layer) -> bool:
        """
        Try Binance then Finnhub. Returns True if a live feed ran (even briefly).
        Exceptions from failed providers are caught here.

        FOUNDATION-09: for symbols on settings.MARKET_DATA_ROUTER_SYMBOLS
        with settings.MARKET_DATA_ROUTER_ENABLED=True, the initial provider
        choice is delegated to the new ProviderRouter pipeline instead of
        this method's own hardcoded order — see _try_live_via_new_router().
        Any failure in that path (selection error, unrecognized provider,
        or the dispatched loop itself failing) falls back to running the
        complete, unmodified legacy body below. Every other symbol, and
        every symbol when the flag is off, never touches that path at all.
        """
        if self._should_use_new_router(symbol):
            try:
                return await self._try_live_via_new_router(symbol, channel_layer)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error(
                    "event=market_data_router_selection_error symbol=%s error=%r "
                    "— falling back to legacy _try_live",
                    symbol, exc,
                )

        return await self._try_live_legacy(symbol, channel_layer)

    def _should_use_new_router(self, symbol: str) -> bool:
        """Flag + allowlist gate. Never raises — any settings-access problem
        is treated as "no", same as the flag being off."""
        try:
            from django.conf import settings
            if not getattr(settings, "MARKET_DATA_ROUTER_ENABLED", False):
                return False
            allowlist = getattr(settings, "MARKET_DATA_ROUTER_SYMBOLS", frozenset())
            return symbol in allowlist
        except Exception:
            return False

    def _observability_enabled(self) -> bool:
        """FOUNDATION-13 flag gate for market_data/observability/ recording
        hooks — independent of _should_use_new_router(), applies to every
        symbol (legacy or router-controlled). Never raises."""
        try:
            from django.conf import settings
            return bool(getattr(settings, "MARKET_DATA_OBSERVABILITY_ENABLED", False))
        except Exception:
            return False

    async def _try_live_via_new_router(self, symbol: str, channel_layer) -> bool:
        """
        Called only when _should_use_new_router(symbol) is True. Builds a
        fresh selection via market_data.runtime_router and dispatches to
        the SAME existing loop methods legacy uses below — no new
        connection, retry, or backoff logic.

        Raising here is the intended way to signal "give up on the router
        path" to the caller, which catches everything and falls back to
        _try_live_legacy(). Covers a router selection failure, an
        unrecognized provider_id, and a real connection failure in the
        dispatched loop uniformly — all become the same outcome.

        FOUNDATION-11: before asking the router to pick a provider, check
        whether the market is even open. If it isn't, this returns False
        immediately — same outcome as "no live provider" — without ever
        calling select_runtime_provider() or touching the circuit breaker.
        A closed market during off-hours is not a provider failure; it must
        never be recorded as one.
        """
        from market_data.sessions import MarketSessionState
        from market_data.sessions.service import evaluate_market_session_for_symbol

        session = evaluate_market_session_for_symbol(symbol)
        log.info(
            "event=market_data_market_session symbol=%s calendar_id=%s state=%s "
            "order_policy=%s reason_code=%s next_open_at=%s next_close_at=%s",
            session.canonical_symbol, session.calendar_id.value, session.state.value,
            session.order_policy.value, session.reason_code.value,
            session.next_open_at.isoformat() if session.next_open_at else None,
            session.next_close_at.isoformat() if session.next_close_at else None,
        )
        if self._observability_enabled():
            try:
                from market_data.observability import record_session_state
                record_session_state(symbol, session.state)
            except Exception as exc:
                log.debug("[observability] session recording failed for %s (non-fatal): %r", symbol, exc)
        if session.state != MarketSessionState.OPEN:
            # Market closed (or unknown) — do not attempt a provider, do not
            # penalize any breaker. Let _feed_loop's existing sim fallback
            # provide continuity, exactly as it already does for "no
            # provider selected".
            return False

        from market_data.runtime_router.service import select_runtime_provider
        from market_data.runtime_router.state import (
            record_provider_failure,
            record_provider_success,
            record_selection,
        )

        decision = select_runtime_provider(symbol)

        log.info(
            "event=market_data_router_selection symbol=%s selected_provider=%s "
            "used_new_router=%s fallback_to_legacy=%s reason_code=%s error_code=%s",
            decision.symbol, decision.selected_provider_id, decision.used_new_router,
            decision.fallback_to_legacy,
            decision.reason_code.value if decision.reason_code else None,
            decision.error_code,
        )

        if decision.fallback_to_legacy:
            raise RuntimeError(f"router could not produce a decision: error_code={decision.error_code!r}")

        record_selection(symbol, decision.selected_provider_id, decision.reason_code)

        if self._observability_enabled():
            try:
                from market_data.observability import record_selection as _record_observability_selection
                _record_observability_selection(
                    symbol,
                    provider_id=decision.selected_provider_id,
                    provider_symbol=decision.selected_provider_symbol,
                    source_state=decision.source_state,
                    order_policy=decision.order_policy,
                    degraded=decision.degraded,
                    reason_code=decision.reason_code,
                )
            except Exception as exc:
                log.debug("[observability] selection recording failed for %s (non-fatal): %r", symbol, exc)

        provider_id = decision.selected_provider_id
        if provider_id is None:
            # A valid, successful decision: no live provider available.
            # Let _feed_loop's existing sim fallback take over, unchanged.
            return False

        # FOUNDATION-10: feedback hooks — record_provider_success fires once,
        # on the first valid tick (not on socket connect); record_provider_failure
        # fires once, only when the dispatched loop gives up for a real error
        # (never for CancelledError — that's handled by the caller's own
        # except asyncio.CancelledError: raise, which this dispatch never
        # intercepts). Both are already exception-safe on their own (see
        # market_data/runtime_router/state.py) — the loops wrap the calls too,
        # belt-and-suspenders, since these run inside a live feed loop.
        def _on_success() -> None:
            record_provider_success(symbol, provider_id)
            if self._observability_enabled():
                try:
                    from market_data.observability import record_first_tick
                    record_first_tick(symbol, provider_id)
                except Exception as obs_exc:
                    log.debug("[observability] first-tick recording failed for %s (non-fatal): %r", symbol, obs_exc)

        def _on_failure(exc: Exception) -> None:
            record_provider_failure(symbol, provider_id, error_code=repr(exc))
            if self._observability_enabled():
                try:
                    from market_data.observability import record_terminal_failure
                    record_terminal_failure(symbol, provider_id, error_code=repr(exc))
                except Exception as obs_exc:
                    log.debug("[observability] terminal-failure recording failed for %s (non-fatal): %r", symbol, obs_exc)

        # Explicit, testable provider -> existing-loop dispatch. Never
        # execute anything the router didn't name here.
        dispatch = {
            "binance": lambda: self._binance_loop(
                symbol, decision.selected_provider_symbol, channel_layer,
                on_first_tick=_on_success, on_terminal_failure=_on_failure,
            ),
            "kraken": lambda: self._kraken_loop(
                symbol, decision.selected_provider_symbol, channel_layer,
                on_first_tick=_on_success, on_terminal_failure=_on_failure,
            ),
            "finnhub": lambda: self._finnhub_loop(
                symbol, channel_layer,
                on_first_tick=_on_success, on_terminal_failure=_on_failure,
            ),
        }
        loop_call = dispatch.get(provider_id)
        if loop_call is None:
            raise RuntimeError(f"router selected unrecognized provider_id={provider_id!r}")

        await loop_call()
        return True

    async def _try_live_legacy(self, symbol: str, channel_layer) -> bool:
        """
        Original _try_live logic — Binance -> Kraken -> Finnhub, hardcoded
        order. Untouched by FOUNDATION-09: this is what every symbol runs
        when the router flag is off, and what any symbol runs when it's
        not on the allowlist, and what an allowlisted symbol falls back to
        on any router-path failure.
        """
        mapped = _binance_sym(symbol)

        if mapped and websockets:
            try:
                await self._binance_loop(symbol, mapped, channel_layer)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("[feed] Binance failed for %s (%r)", symbol, exc)

        kr_pair = _kraken_sym(symbol)
        if kr_pair and websockets:
            try:
                await self._kraken_loop(symbol, kr_pair, channel_layer)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("[feed] Kraken failed for %s (%r)", symbol, exc)

        if FINNHUB_API_KEY and websockets and "/" in symbol:
            try:
                await self._finnhub_loop(symbol, channel_layer)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("[feed] Finnhub failed for %s (%r)", symbol, exc)

        return False

    # ── sim loop (bounded) ──

    async def _sim_loop(self, symbol: str, channel_layer, duration: int | None = None) -> None:
        """
        Walk price randomly. Exits after `duration` seconds if set.
        Resyncs to real market every SIM_RESYNC_INTERVAL seconds.
        """
        log.info("[feed] sim loop %s (duration=%s)", symbol, duration)
        interval   = DEFAULT_TICK_INTERVAL
        _, dec     = _step_dec(symbol)
        deadline   = time.monotonic() + duration if duration else None
        resync_at  = time.monotonic() + SIM_RESYNC_INTERVAL

        while True:
            now = time.monotonic()

            if deadline and now >= deadline:
                break

            if now >= resync_at:
                await self._resync_price(symbol)
                resync_at = now + SIM_RESYNC_INTERVAL

            try:
                mid = self._prices.get(symbol) or _fallback_price(symbol)
                mid += (random.random() - 0.5) * _drift(symbol)
                spr = _spread(symbol)
                bid = round(mid - spr / 2, dec)
                ask = round(mid + spr / 2, dec)
                await self._broadcast(symbol, channel_layer, bid, ask, int(time.time()), source="sim")
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("[feed] sim error %s: %r", symbol, exc)
                await asyncio.sleep(1)

    # ── live feed loops ──

    async def _binance_loop(
        self, symbol: str, mapped: str, channel_layer, *,
        on_first_tick: Optional[Callable[[], None]] = None,
        on_terminal_failure: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        """
        FOUNDATION-10: on_first_tick/on_terminal_failure are optional
        feedback hooks for the new router's circuit breaker — None for
        every legacy call site, which leaves this method's connection,
        retry, and backoff logic completely unchanged. on_first_tick fires
        once, the first time a valid bid/ask is broadcast in this
        invocation (not on socket connect). on_terminal_failure fires
        once, only when this method is about to give up and re-raise after
        MAX_FAILURES reconnect attempts — never for asyncio.CancelledError.
        Both are wrapped so a bug in feedback recording can never affect
        the real feed.
        """
        url = (
            f"wss://stream.binance.com:9443/stream"
            f"?streams={mapped}@bookTicker/{mapped}@kline_1m"
        )
        log.info("[feed] Binance loop for %s (%s)", symbol, mapped)
        consecutive_failures = 0
        MAX_FAILURES = 3
        tick_reported = False
        while True:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20, ping_timeout=20,
                    close_timeout=10, max_queue=256,
                ) as ws:
                    consecutive_failures = 0
                    async for raw in ws:
                        obj    = json.loads(raw)
                        stream = obj.get("stream") or ""
                        data   = obj.get("data")   or {}
                        if stream.endswith("@bookTicker"):
                            b = float(data.get("b") or 0.0)
                            a = float(data.get("a") or 0.0)
                            if a > b > 0:
                                await self._broadcast(symbol, channel_layer, b, a, int(time.time()), source="binance")
                                if not tick_reported and on_first_tick is not None:
                                    tick_reported = True
                                    try:
                                        on_first_tick()
                                    except Exception as cb_exc:
                                        log.debug("[feed] on_first_tick callback failed for %s: %r", symbol, cb_exc)
                        elif stream.endswith("@kline_1m"):
                            k = data.get("k") or {}
                            open_ms = int(k.get("t") or 0)
                            if open_ms > 0:
                                await self._broadcast_kline(symbol, channel_layer, {
                                    "time":      open_ms // 1000,
                                    "open":      float(k["o"]),
                                    "high":      float(k["h"]),
                                    "low":       float(k["l"]),
                                    "close":     float(k["c"]),
                                    "volume":    float(k["v"]),
                                    "is_closed": bool(k.get("x", False)),
                                })
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_failures += 1
                if consecutive_failures >= MAX_FAILURES:
                    log.warning(
                        "[feed] Binance giving up for %s after %d failures",
                        symbol, consecutive_failures,
                    )
                    if on_terminal_failure is not None:
                        try:
                            on_terminal_failure(exc)
                        except Exception as cb_exc:
                            log.debug("[feed] on_terminal_failure callback failed for %s: %r", symbol, cb_exc)
                    raise
                log.error(
                    "[feed] Binance error %s: %r — reconnect in 3s (%d/%d)",
                    symbol, exc, consecutive_failures, MAX_FAILURES,
                )
                await asyncio.sleep(3)

    async def _kraken_loop(
        self, symbol: str, kr_pair: str, channel_layer, *,
        on_first_tick: Optional[Callable[[], None]] = None,
        on_terminal_failure: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        """
        Kraken WS v1 — provides both ticker (bid/ask) and ohlc-1 (1m candles).
        Used as fallback when Binance WS is unavailable.
        Candles are broadcast via _broadcast_kline so candle_kline() picks them up.

        FOUNDATION-10: see _binance_loop's docstring for on_first_tick /
        on_terminal_failure semantics — identical contract here.
        """
        url = "wss://ws.kraken.com"
        log.info("[feed] Kraken loop for %s (%s)", symbol, kr_pair)
        consecutive_failures = 0
        MAX_FAILURES = 3
        tick_reported = False

        while True:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20, ping_timeout=20,
                    close_timeout=10, max_queue=256,
                ) as ws:
                    consecutive_failures = 0
                    await ws.send(json.dumps({
                        "event": "subscribe",
                        "pair":  [kr_pair],
                        "subscription": {"name": "ticker"},
                    }))
                    await ws.send(json.dumps({
                        "event": "subscribe",
                        "pair":  [kr_pair],
                        "subscription": {"name": "ohlc", "interval": 1},
                    }))
                    async for raw in ws:
                        msg = json.loads(raw)
                        if not isinstance(msg, list):
                            continue  # heartbeat / subscription status dicts
                        if len(msg) < 4:
                            continue
                        channel_name = msg[-2] if isinstance(msg[-2], str) else ""
                        data = msg[1]

                        if channel_name == "ticker":
                            bid = float(data["b"][0])
                            ask = float(data["a"][0])
                            if ask > bid > 0:
                                await self._broadcast(symbol, channel_layer, bid, ask, int(time.time()), source="kraken")
                                if not tick_reported and on_first_tick is not None:
                                    tick_reported = True
                                    try:
                                        on_first_tick()
                                    except Exception as cb_exc:
                                        log.debug("[feed] on_first_tick callback failed for %s: %r", symbol, cb_exc)

                        elif channel_name.startswith("ohlc"):
                            # row: [time, etime, open, high, low, close, vwap, volume, count]
                            raw_t = float(data[0])
                            # Align to 1-minute bucket boundary (Kraken time is trade time, not bar open)
                            bucket = (int(raw_t) // 60) * 60
                            await self._broadcast_kline(symbol, channel_layer, {
                                "time":      bucket,
                                "open":      float(data[2]),
                                "high":      float(data[3]),
                                "low":       float(data[4]),
                                "close":     float(data[5]),
                                "volume":    float(data[7]),
                                "is_closed": False,  # Kraken doesn't signal close explicitly
                            })

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_failures += 1
                if consecutive_failures >= MAX_FAILURES:
                    log.warning("[feed] Kraken giving up for %s after %d failures", symbol, consecutive_failures)
                    if on_terminal_failure is not None:
                        try:
                            on_terminal_failure(exc)
                        except Exception as cb_exc:
                            log.debug("[feed] on_terminal_failure callback failed for %s: %r", symbol, cb_exc)
                    raise
                log.error("[feed] Kraken error %s: %r — reconnect in 3s (%d/%d)", symbol, exc, consecutive_failures, MAX_FAILURES)
                await asyncio.sleep(3)

    async def _finnhub_loop(
        self, symbol: str, channel_layer, *,
        on_first_tick: Optional[Callable[[], None]] = None,
        on_terminal_failure: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        """FOUNDATION-10: see _binance_loop's docstring for on_first_tick /
        on_terminal_failure semantics — identical contract here."""
        finnhub_sym = _finnhub_sym(symbol)
        url = f"wss://ws.finnhub.io?token={FINNHUB_API_KEY}"
        _, dec = _step_dec(symbol)
        log.info("[feed] Finnhub loop for %s (%s)", symbol, finnhub_sym)
        consecutive_failures = 0
        MAX_FAILURES = 3
        tick_reported = False
        while True:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20, ping_timeout=20,
                    close_timeout=10, max_queue=256,
                ) as ws:
                    await ws.send(json.dumps({"type": "subscribe", "symbol": finnhub_sym}))
                    async for raw in ws:
                        msg = json.loads(raw)
                        msg_type = msg.get("type")
                        if msg_type == "error":
                            # O.6c-1ae — Finnhub protocol-level error
                            # (invalid symbol, auth, rate-limit, ...).
                            # The connection stays open and no "trade"
                            # will ever follow for a rejected
                            # subscription — silently `continue`-ing
                            # past this (pre-O.6c-1ae behavior) left
                            # this loop parked in `async for raw in ws`
                            # forever: no exception, no _broadcast, no
                            # fallback to sim. Raising here feeds the
                            # SAME consecutive_failures/MAX_FAILURES/
                            # on_terminal_failure machinery below as a
                            # real connection failure, so this gives up
                            # and lets _try_live_legacy fall through to
                            # the sim loop within a bounded number of
                            # attempts instead of hanging indefinitely.
                            err_text = msg.get("msg", "")
                            log.error(
                                "[feed] Finnhub protocol error for %s (%s): %s",
                                symbol, finnhub_sym, err_text,
                            )
                            raise RuntimeError(
                                f"finnhub_protocol_error symbol={finnhub_sym}: {err_text}"
                            )
                        if msg_type != "trade":
                            continue
                        for t in msg.get("data", []):
                            px = float(t.get("p") or 0.0)
                            if not px:
                                continue
                            spr = _spread(symbol)
                            bid = round(px - spr / 2, dec)
                            ask = round(px + spr / 2, dec)
                            ts  = int((t.get("t") or time.time() * 1000) / 1000)
                            await self._broadcast(symbol, channel_layer, bid, ask, ts, source="finnhub")
                            # O.6c-1ae — reset only on a genuine trade, not
                            # on a bare successful TCP/WS handshake (moved
                            # off the `async with` entry above). A symbol
                            # Finnhub permanently rejects at the protocol
                            # level (bad mapping, revoked auth) reconnects
                            # successfully every single attempt — resetting
                            # here on connect alone made consecutive_
                            # failures never accumulate past 1, so
                            # MAX_FAILURES was never reached and this loop
                            # retried forever instead of giving up and
                            # letting _try_live_legacy fall back to sim.
                            consecutive_failures = 0
                            if not tick_reported and on_first_tick is not None:
                                tick_reported = True
                                try:
                                    on_first_tick()
                                except Exception as cb_exc:
                                    log.debug("[feed] on_first_tick callback failed for %s: %r", symbol, cb_exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_failures += 1
                if consecutive_failures >= MAX_FAILURES:
                    log.warning(
                        "[feed] Finnhub giving up for %s after %d failures",
                        symbol, consecutive_failures,
                    )
                    if on_terminal_failure is not None:
                        try:
                            on_terminal_failure(exc)
                        except Exception as cb_exc:
                            log.debug("[feed] on_terminal_failure callback failed for %s: %r", symbol, cb_exc)
                    raise
                log.error(
                    "[feed] Finnhub error %s: %r — reconnect in 3s (%d/%d)",
                    symbol, exc, consecutive_failures, MAX_FAILURES,
                )
                await asyncio.sleep(3)

    # ── price resync via REST ──

    async def _resync_price(self, symbol: str) -> None:
        """
        Fetch current mid price via REST and snap internal state.
        Runs in a thread executor (urllib, no extra deps).
        """
        price = await self._fetch_rest_price(symbol)
        if price and price > 0:
            _, dec = _step_dec(symbol)
            spr    = _spread(symbol)
            mid    = round(price, dec)
            with self._lock:
                self._prices[symbol]       = mid
                self._bids[symbol]         = round(mid - spr / 2, dec)
                self._asks[symbol]         = round(mid + spr / 2, dec)
                self._price_ts[symbol]     = time.time()
                self._price_source[symbol] = "rest_resync"  # O.6c-1w
            log.info("[feed] resynced %s → %.4f", symbol, mid)
        else:
            # Keep whatever we have; only fall to hardcoded if nothing stored.
            # Deliberately does NOT set _price_ts — a fallback price is a
            # static per-symbol default, not a real quote; has_price() must
            # never treat it as fresh (see has_price()'s docstring).
            with self._lock:
                seeded_fallback = symbol not in self._prices
                if seeded_fallback:
                    self._prices[symbol] = _fallback_price(symbol)
                fallback_value = self._prices.get(symbol)
            if seeded_fallback:
                log.warning(
                    "[feed] REST resync failed for %s — using fallback %.2f",
                    symbol, fallback_value,
                )

    async def _fetch_rest_price(self, symbol: str) -> float | None:
        """
        Non-blocking HTTP REST price fetch.
        Tries multiple sources in order — first success wins.
        Crypto: Binance → CoinGecko → Kraken (all free, no key needed for CG/Kraken)
        FX:     Finnhub REST (requires API key)
        Note: Binance REST may be geo-blocked in some regions; CoinGecko/Kraken are fallbacks.
        """
        loop = asyncio.get_event_loop()

        def _fetch(url: str) -> bytes:
            req = urllib.request.Request(url, headers={"User-Agent": "trx-sim/1.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.read()

        mapped = _binance_sym(symbol)

        # ── Crypto: Binance REST ──
        if mapped:
            try:
                data = json.loads(await loop.run_in_executor(
                    None, _fetch, f"https://api.binance.com/api/v3/ticker/price?symbol={mapped}"
                ))
                px = float(data["price"])
                log.debug("[feed] Binance REST %s = %.4f", symbol, px)
                return px
            except Exception as exc:
                log.debug("[feed] Binance REST unavailable for %s: %r", symbol, exc)

        # ── Crypto: CoinGecko (free, no key, rarely geo-blocked) ──
        _CG_IDS = {"BTCUSD": "bitcoin", "ETHUSD": "ethereum"}
        cg_id = _CG_IDS.get(symbol)
        if cg_id:
            try:
                data = json.loads(await loop.run_in_executor(
                    None, _fetch,
                    f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd",
                ))
                px = float(data[cg_id]["usd"])
                log.debug("[feed] CoinGecko REST %s = %.4f", symbol, px)
                return px
            except Exception as exc:
                log.debug("[feed] CoinGecko REST unavailable for %s: %r", symbol, exc)

        # ── Crypto: Kraken (free, no key) ──
        _KR_PAIRS = {"BTCUSD": "XBTUSD", "ETHUSD": "XETHZUSD"}
        kr_pair = _KR_PAIRS.get(symbol)
        if kr_pair:
            try:
                data = json.loads(await loop.run_in_executor(
                    None, _fetch,
                    f"https://api.kraken.com/0/public/Ticker?pair={kr_pair}",
                ))
                result = data.get("result") or {}
                ticker = next(iter(result.values()), None) if result else None
                if ticker:
                    px = float(ticker["c"][0])
                    log.debug("[feed] Kraken REST %s = %.4f", symbol, px)
                    return px
            except Exception as exc:
                log.debug("[feed] Kraken REST unavailable for %s: %r", symbol, exc)

        # ── FX: Finnhub REST ──
        if FINNHUB_API_KEY and "/" in symbol:
            fh_sym = _finnhub_sym(symbol)
            url    = (f"https://finnhub.io/api/v1/quote"
                      f"?symbol={fh_sym}&token={FINNHUB_API_KEY}")
            try:
                data = json.loads(await loop.run_in_executor(None, _fetch, url))
                px   = float(data.get("c") or 0)
                if px > 0:
                    log.debug("[feed] Finnhub REST %s = %.5f", symbol, px)
                    return px
            except Exception as exc:
                log.debug("[feed] Finnhub REST unavailable for %s: %r", symbol, exc)

        log.warning("[feed] all REST sources failed for %s", symbol)
        return None
