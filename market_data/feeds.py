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
MASSIVE_API_KEY       = (os.getenv("MASSIVE_API_KEY", "") or "").strip()
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


# ─── FIX-05B.2 — Massive (Forex Historical ONLY) ────────────────────────────────
#
# FIX-05B.2-B design lock. Scope: REST historical aggregates for exactly the
# 4 symbols below, never WS, never quote/resync, never XAU/USD (ticker
# candidate C:XAUUSD was confirmed live in FIX-05B.2-A.1 but deliberately
# not enabled here — that's a future, separately-authorized block). Crypto
# history (fetch_kline_history, below) is completely untouched — these are
# two disjoint symbol sets by construction (kline_symbols() vs this
# allowlist), never merged into one provider-selection framework.
_MASSIVE_ENABLED_SYMBOLS = frozenset({"EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD"})

# multiplier, timespan — Massive's own /v2/aggs/ticker/.../range/{mult}/{span}/...
# path segments. Single source of truth; consumers.py never duplicates this.
_MASSIVE_TF = {
    "1s":  (1, "second"),
    "1m":  (1, "minute"),
    "5m":  (5, "minute"),
    "15m": (15, "minute"),
    "1h":  (1, "hour"),
    "1d":  (1, "day"),
}
_MASSIVE_SECONDS_PER_UNIT = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}

# FIX-05B.2-A.1 — Massive's own 1h coverage is confirmed real but uneven
# across pairs (EUR/USD can legitimately return HTTP 200/status=OK/
# resultsCount=0 over a 30-day window while GBP/USD returns a real bar in
# the same window) — GAP_BUFFER exists to absorb ordinary weekend/gap
# density, not to paper over that specific finding; a genuinely empty
# result after this buffer is a legitimate provider_unavailable, not a
# bug (see fetch_massive_history's error contract below).
GAP_BUFFER = 2.0
_MASSIVE_RANGE_MIN_SECONDS = 86400
_MASSIVE_RANGE_MAX_SECONDS = 400 * 86400

# CHART-GLOBAL-REGRESSION-01 — precheck confirmed live (real Massive API,
# sort=desc, 2026-09-03): the aggs endpoint returns a small, timeframe-
# driven page size for EVERY Forex pair tested (EUR/USD, GBP/USD, USD/JPY,
# AUD/USD all identical) — 200/39/13/3/203 results per page for
# 1m/5m/15m/1h/1d respectively, regardless of the requested `limit`. The
# original _MASSIVE_MAX_PAGES=3 (1 initial + 2 continuations) was only
# ever validated against 1m (which happens to fit in a single page) —
# 15m/1h silently truncated Forex history to 3-16 DAYS stale, for every
# pair, undetected until this audit. Renamed (not duplicated) to
# _MASSIVE_FOREX_MAX_PAGES and raised to 20 — same evidence-backed value
# already proven for _MASSIVE_CRYPTO_MAX_PAGES, since the two page-size
# profiles are identical. Worst case (1h): 20 pages ≈ 60-80 fresh bars
# instead of 200 stale ones — FRESHNESS > DEPTH, same tradeoff as crypto.
_MASSIVE_FOREX_MAX_PAGES = 20


def _massive_sym(symbol: str) -> "str | None":
    """'EUR/USD' -> 'C:EURUSD'. None for anything outside the allowlist —
    never a 'contains /' heuristic (FIX-05B.2-B design lock §2)."""
    if symbol not in _MASSIVE_ENABLED_SYMBOLS:
        return None
    return f"C:{symbol.replace('/', '')}"


def _massive_range(interval: str, bars: int) -> "tuple[str, str] | None":
    """(from_date, to_date) as UTC YYYY-MM-DD strings, sized to comfortably
    contain `bars` closed candles of `interval`. Pure date-range sizing —
    never a loop, never re-queried/widened here (that's what bounded
    pagination in fetch_massive_history is for). Returns None for an
    unsupported interval (caller fails closed)."""
    tf = _MASSIVE_TF.get(interval)
    if tf is None:
        return None
    multiplier, timespan = tf
    bar_seconds = multiplier * _MASSIVE_SECONDS_PER_UNIT[timespan]
    raw_span_seconds = bar_seconds * max(int(bars), 1)
    span_seconds = raw_span_seconds * GAP_BUFFER
    span_seconds = max(_MASSIVE_RANGE_MIN_SECONDS, min(span_seconds, _MASSIVE_RANGE_MAX_SECONDS))

    from datetime import datetime, timedelta, timezone as _dt_tz
    to_dt = datetime.now(_dt_tz.utc)
    from_dt = to_dt - timedelta(seconds=span_seconds)
    return from_dt.strftime("%Y-%m-%d"), to_dt.strftime("%Y-%m-%d")


# CHART-HISTORY-INSTANT-LOAD-01 — first-paint split. Massive's per-page
# result count (confirmed live, both Forex and crypto) is far smaller
# than the requested `limit` for anything past 1m, so reaching full depth
# can take up to _MASSIVE_FOREX_MAX_PAGES/_MASSIVE_CRYPTO_MAX_PAGES
# sequential REST round-trips (measured: ~3.6s for Forex 15m, ~5.0s for
# 1h) — all of it previously spent BEFORE generate_history() had
# anything to send, blocking the whole TradingConsumer (Channels
# processes one event per connection at a time) for that entire window.
# fetch_massive_history()/fetch_massive_crypto_history() are now each
# split into a `_first_page` (exactly one REST call, returns immediately)
# and a `_remaining` (continues from a cursor, used from a detached
# asyncio.Task in consumers.py so the consumer is never blocked past the
# first ~0.3s) — the two together produce byte-identical output to the
# original single-call function, which is kept as a thin composition of
# both for 100% backward compatibility with every existing caller/test.
@dataclass
class _MassiveHistoryCursor:
    """Opaque resume state for continuing a Massive aggs pagination past
    page 1. Only ever produced by a `_first_page` fetcher and consumed by
    its matching `_remaining` fetcher — callers never inspect its fields."""
    symbol: str
    interval: str
    limit: int
    bars_so_far: list  # raw parsed rows from page 1, DESC arrival order
    pages_so_far: int
    next_url: str


def _finalize_massive_bars(bars: list, limit: int) -> list:
    """Shared by all 4 Massive history fetchers (Forex/Crypto x first-
    page/remaining): dedupe by timestamp in arrival (DESC) order (first
    occurrence wins), trim to the most recent `limit` entries, sort
    chronologically ascending — the single final-shape contract every
    history fetcher in this file returns. Pure data reshaping (no
    network/provider logic), so sharing it across the Forex/Crypto
    boundary does not reopen Design Lock Option A's "duplicate, don't
    share" decision — that decision is about the live-connection/
    broadcast code, not this stateless post-processing step (already
    byte-identical in both before this split existed)."""
    seen: set = set()
    deduped_desc = []
    for b in bars:
        if b["time"] not in seen:
            seen.add(b["time"])
            deduped_desc.append(b)
    trimmed = deduped_desc[:limit]
    return sorted(trimmed, key=lambda x: x["time"])


# ─── FIX-05B.3-B1/B2 — Massive Forex LIVE WebSocket ─────────────────────────────
#
# Design lock: FIX-05B.3-A (B1), FIX-05B.3-B2-A (B2). B2 scope: all 4
# symbols share ONE Massive connection (see FeedManager._massive_shared_loop
# below) — never one connection per symbol. XAU/USD stays out of scope
# entirely (same as historical, FIX-05B.2). Crypto is untouched —
# Binance/Kraken are checked first in _try_live_legacy(), same as always.
#
# wss://socket.massive.com/forex — confirmed live (GOLDEN-LIVE-PROVIDER-01,
# FIX-05B.3-A): auth-then-subscribe, one JSON array of event dicts per
# message, {"ev":"C","p":"EUR/USD","b":bid,"a":ask,"t":ms} for a live quote.
# "C.EUR/USD" (dot, WITH slash) is the WS subscribe param — never confused
# with "C:EURUSD" (colon, no slash), the unrelated historical aggs ticker
# format _massive_sym() already owns (FIX-05B.2). Batch subscribe (initial
# connect/reconnect) comma-joins params for every currently active symbol
# — confirmed live (GOLDEN-LIVE-PROVIDER-01: "1-connection-4-symbols
# works").
_MASSIVE_WS_URL = "wss://socket.massive.com/forex"
_MASSIVE_WS_ENABLED_SYMBOLS = frozenset({"EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD"})  # B2

# FIX-05B.3-B1.1/B2 — data-staleness watchdog. GOLDEN-MASSIVE-PRICECACHE-01
# found the WS connection can stay technically alive (ping/pong healthy,
# no exception) while silently stopping delivery of "ev":"C" quote
# events for minutes — a failure mode connection-error handling alone
# never detects. DATA_STALE_TIMEOUT (20s) is deliberately well under
# _PRICE_CACHE_TTL (60s, feeds.py, UNCHANGED by this block) — chosen
# from live evidence (GOLDEN-LIVE-PROVIDER-01: AUD/USD, the sparsest of
# the 4 pairs, ticked at worst every ~3.3s under normal conditions; 20s
# is ~6x that gap, and still leaves 40s of TTL headroom for the
# reconnect+auth+resubscribe cycle — sub-second in every live test so
# far — to recover the feed before get_validated_quote() would fail
# closed on staleness anyway). Design Lock FIX-05B.3-B2-A §8 — the SAME
# 20s threshold is deliberately reused, unchanged, for both the
# connection-level and the per-symbol checks: no evidence yet that any
# of the 4 pairs needs a different value, and a second threshold knob
# without evidence is premature complexity.
DATA_STALE_TIMEOUT = 20.0
_MASSIVE_WATCHDOG_POLL_SECONDS = 5.0

# FIX-05B.3-B2-A §0.A — this many CONSECUTIVE per-symbol stale incidents
# (each incident is a full DATA_STALE_TIMEOUT window with no valid quote
# for that symbol, even after a resubscribe attempt on the first one)
# before the watchdog's log severity steps up from WARNING to ERROR.
# 1 resubscribe absorbs a transient blip (AUD/USD already showed sparser
# ticks than EUR/USD in GOLDEN-LIVE-PROVIDER-01); persisting past a
# SECOND full window while its siblings on the same connection stay
# healthy is strong evidence the fault is specific to that symbol's
# subscription, not the connection — worth a louder log, but still just
# another resubscribe. B2-FOREX-PROVIDER-CLEANUP-01 §3/§E removed the
# escalation-to-Finnhub action this threshold used to trigger: Massive
# is the sole runtime provider for these 4 pairs, so it keeps retrying
# indefinitely instead — this constant is now purely an observability
# knob, unchanged in value per that Design Lock's explicit §11.
_MASSIVE_SYMBOL_STALE_ESCALATION_THRESHOLD = 2


def _massive_ws_param(symbol: str) -> str:
    """'EUR/USD' -> 'C.EUR/USD'."""
    return f"C.{symbol}"


def _massive_ws_params_batch(symbols) -> str:
    """Comma-joined subscribe params for every symbol in *symbols*, sorted
    for deterministic ordering (Massive does not care about order; tests
    and logs do). '' for an empty iterable — callers must never send an
    empty subscribe."""
    return ",".join(_massive_ws_param(s) for s in sorted(symbols))


def _parse_massive_events(raw) -> list:
    """Massive sends a JSON array of event dicts per WS message (confirmed
    live). A single malformed/unexpected message must never kill the
    reader — returns [] on any parse failure, same fail-soft contract
    _parse_binance()-style helpers already follow elsewhere in this file."""
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    if isinstance(data, dict):
        return [data]
    return []


# ─── GOLDEN-MARKETDATA-CRYPTO-01 — Massive Crypto (historical + live) ───────────
#
# Design Lock: GOLDEN_MARKETDATA_CRYPTO_01_DESIGN_LOCK.md, Option A (approved) —
# deliberately duplicates the FIX-05B.2/FIX-05B.3-B2-A Forex-Massive pattern
# with a `_crypto_` infix on every new symbol/state/method, rather than
# refactoring/sharing code with the certified Forex implementation — this
# minimizes Forex regression risk (Forex code below is untouched). One
# unified allowlist covers historical, live WS, and REST resync — unlike
# Forex there is no disabled-but-historical-only crypto symbol today.
#
# wss://socket.massive.com/crypto is a DIFFERENT physical cluster from
# Forex's wss://socket.massive.com/forex — confirmed live
# (GOLDEN-MARKETDATA-CRYPTO-01 provider-access audit): cannot share one
# connection with Forex, hence the fully parallel shared-connection state
# below (mirrors _massive_* 1:1, never merged with it).
_MASSIVE_WS_CRYPTO_URL = "wss://socket.massive.com/crypto"
_MASSIVE_CRYPTO_ENABLED_SYMBOLS = frozenset({"BTCUSD", "ETHUSD"})

# GOLDEN-MARKETDATA-CRYPTO-01 — ACCEPTANCE FIX (pagination). Confirmed live
# against the real Massive REST API: the crypto aggs endpoint returns only
# ~13 results per page for 15m regardless of the requested `limit` (at the
# time this was written, Forex's own 1m had only been checked at a single
# page — CHART-GLOBAL-REGRESSION-01 later confirmed live that Forex has
# the SAME small, timeframe-driven page size for every pair/timeframe
# except 1m, and needed the identical fix — see _MASSIVE_FOREX_MAX_PAGES
# and fetch_massive_history() below). Reusing the old 3-page cap for
# crypto silently truncated history ~4 days short of "now" (BTCUSD/ETHUSD
# 15m both landed on the exact same stale timestamp — a systemic, not
# per-symbol, gap). `sort=desc` (also confirmed live) makes page 1 always
# contain the NEWEST bars regardless of how many pages are ultimately
# fetched, so freshness degrades gracefully with this cap even for the
# sparsest timeframe (1h: only ~60 of a requested 200 bars fit in 20
# pages, but the newest bar is still <1h old) — deliberately preferring
# less depth over stale-but-"complete" data. This constant stays
# crypto-only (Design Lock Option A — duplicate, don't share); Forex has
# its own separate constant with the same value, not this one.
_MASSIVE_CRYPTO_MAX_PAGES = 20

# Confirmed live: WS subscribe channel prefix is "XQ." (not Forex's "C."),
# and the pair spelling uses a hyphen, not the canonical no-separator
# internal symbol ("BTC-USD", never "BTCUSD") — an explicit dict, never a
# "insert a hyphen" heuristic, same defensive posture as _KRAKEN_REST_PAIR
# above. REST ticker format instead has NO separator to insert (Polygon-
# style crypto prefix "X:" + the canonical symbol as-is: "X:BTCUSD").
_MASSIVE_CRYPTO_WS_PAIR = {"BTCUSD": "BTC-USD", "ETHUSD": "ETH-USD"}
_MASSIVE_CRYPTO_WS_PAIR_REVERSE = {v: k for k, v in _MASSIVE_CRYPTO_WS_PAIR.items()}


def _massive_crypto_sym(symbol: str) -> "str | None":
    """'BTCUSD' -> 'X:BTCUSD'. None for anything outside the allowlist —
    never a heuristic (mirrors _massive_sym())."""
    if symbol not in _MASSIVE_CRYPTO_ENABLED_SYMBOLS:
        return None
    return f"X:{symbol}"


def _massive_crypto_ws_pair(symbol: str) -> "str | None":
    """'BTCUSD' -> 'BTC-USD'. None for anything outside the allowlist."""
    return _MASSIVE_CRYPTO_WS_PAIR.get(symbol)


def _massive_crypto_symbol_from_pair(pair: str) -> "str | None":
    """'BTC-USD' -> 'BTCUSD' — inverse of _massive_crypto_ws_pair(), used to
    route an incoming "ev":"XQ" event (keyed by Massive's own pair
    spelling) back to the canonical internal symbol. None for an unknown
    pair — the caller drops the event, never guesses."""
    return _MASSIVE_CRYPTO_WS_PAIR_REVERSE.get(pair)


def _massive_crypto_ws_param(symbol: str) -> "str | None":
    """'BTCUSD' -> 'XQ.BTC-USD,XT.BTC-USD' — MASSIVE-CRYPTO-TRADE-
    CANDLES-01: every subscribe/unsubscribe for a crypto symbol now
    covers BOTH the quote channel (XQ — execution/spread/PnL/margin/
    risk/Redis price cache authority, unchanged) and the trade channel
    (XT — chart/candle/volume only, see _broadcast_trade()) in the SAME
    call, on the SAME shared connection — never a second socket. Every
    caller (register/unregister/reconnect-resubscribe/per-symbol stale-
    resubscribe) already just forwards this string verbatim as
    "params", so this single change point covers all of them. None for
    anything outside the allowlist."""
    pair = _massive_crypto_ws_pair(symbol)
    return f"XQ.{pair},XT.{pair}" if pair else None


def _massive_crypto_ws_params_batch(symbols) -> str:
    """Comma-joined subscribe params for every symbol in *symbols* that maps
    to a known pair, sorted for deterministic ordering. '' for an empty/
    all-unknown iterable — callers must never send an empty subscribe."""
    params = [p for p in (_massive_crypto_ws_param(s) for s in sorted(symbols)) if p]
    return ",".join(params)


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


# ─── GOLDEN-WEEKEND-RISK-01 — durable last-known-good market price ─────────────
# Redis (above) is the hot/live cache: 60s TTL, gone on restart, gone once a
# symbol's feed loop is idle. LastKnownMarketPrice (simulator/models.py) is
# the durable twin: one row per symbol, updated at most once every
# _DURABLE_PRICE_WRITE_INTERVAL_SECS from this SAME choke point
# (FeedManager._update_price_state, called by every provider's _broadcast),
# read back only by simulator/broker_exposure.py when a symbol's market
# session is officially MARKET_CLOSED (never as a generic stale-feed
# fallback). See Design Lock GOLDEN-WEEKEND-RISK-01.
#
# _DURABLE_PRICE_VALID_SOURCES mirrors exactly the `source=` strings this
# module actually writes via _broadcast()/_update_price_state() today
# ("massive", "binance", "kraken", "finnhub") — "sim" (the synthetic
# fallback, feeds.py's own _sim_loop) is deliberately excluded: never
# persisted as a durable market price.
_DURABLE_PRICE_VALID_SOURCES = frozenset({"massive", "binance", "kraken", "finnhub"})

# 60s — same window already trusted for Redis freshness (_PRICE_CACHE_TTL)
# and the existing take-snapshots-1m Beat cadence; not a new arbitrary value.
_DURABLE_PRICE_WRITE_INTERVAL_SECS = int(os.getenv("DURABLE_PRICE_WRITE_INTERVAL_SECS", "60"))

# Separate single-thread pool (never share with _redis_write_pool — a slow/
# blocked DB write must never delay a Redis write or vice versa).
_durable_price_write_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="durable_price")


def _write_durable_price_sync(symbol: str, bid: float, ask: float, mid: float,
                               source: str, observed_at) -> None:
    """
    Upsert LastKnownMarketPrice for `symbol`. Called from a thread pool —
    must never raise, and a DB failure here must never mark a price as
    persisted (no swallow-then-pretend-success: the row simply isn't
    written/updated, exactly as if this call never happened).

    Monotonic by observed_at: a delayed write for an OLDER quote can never
    overwrite an already-stored NEWER one (race between two async writes
    completing out of order) — enforced via a single conditional UPDATE
    first, falling back to INSERT only when no row exists yet.
    """
    if source not in _DURABLE_PRICE_VALID_SOURCES:
        return
    try:
        from decimal import Decimal
        from django.db import transaction
        from simulator.models import LastKnownMarketPrice

        bid_d = Decimal(str(bid))
        ask_d = Decimal(str(ask))
        mid_d = Decimal(str(mid))

        with transaction.atomic():
            updated = (
                LastKnownMarketPrice.objects
                .filter(symbol=symbol, observed_at__lte=observed_at)
                .update(bid=bid_d, ask=ask_d, mid=mid_d, source=source, observed_at=observed_at)
            )
            if updated == 0:
                LastKnownMarketPrice.objects.get_or_create(
                    symbol=symbol,
                    defaults={
                        "bid": bid_d, "ask": ask_d, "mid": mid_d,
                        "source": source, "observed_at": observed_at,
                    },
                )
    except Exception as exc:
        # Intentionally swallowed — a DB outage must never crash the feed
        # loop, and must never be reported as a successful persist either.
        log.debug("[durable_price] write failed for %s: %r", symbol, exc)


async def _write_durable_price(symbol: str, bid: float, ask: float, mid: float, source: str) -> None:
    """
    Non-blocking wrapper, mirrors _write_price_cache() exactly. observed_at
    is captured HERE (before dispatch to the thread pool), not inside the
    sync function — so a delayed executor run still carries the timestamp
    of when THIS tick actually arrived, never a later "whenever the thread
    got scheduled" time. That's what makes the monotonic guard in
    _write_durable_price_sync() correct under out-of-order completion.
    """
    from django.utils import timezone as _tz
    observed_at = _tz.now()
    loop = asyncio.get_event_loop()
    loop.run_in_executor(
        _durable_price_write_pool, _write_durable_price_sync,
        symbol, bid, ask, mid, source, observed_at,
    )


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
        # GOLDEN-WEEKEND-RISK-01 — wall-clock time of the last durable-price
        # WRITE ATTEMPT per symbol (not the tick timestamp — see
        # _update_price_state), used only to throttle DB writes to at most
        # once every _DURABLE_PRICE_WRITE_INTERVAL_SECS. Same in-process,
        # per-FeedManager-instance pattern as _price_ts above.
        self._last_durable_write_ts: dict[str, float] = {}
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
        # FIX-05B.3-B1/B2 — Massive Forex live WS shared-connection state.
        # B2 (Design Lock FIX-05B.3-B2-A) makes this genuinely shared: AT
        # MOST ONE Massive connection process-wide, serving every symbol
        # in _MASSIVE_WS_ENABLED_SYMBOLS concurrently. _massive_ws is the
        # live connection object (or None); _massive_subscribed is the
        # set of symbols with an active Massive subscribe on that
        # connection; _massive_connect_lock serializes ws.send() calls
        # (subscribe/unsubscribe) and active-symbol-set mutations across
        # concurrent per-symbol joins/leaves and the shared reader's own
        # reconnect/resubscribe-all sequence — never two sends interleaved.
        self._massive_ws = None
        self._massive_subscribed: set[str] = set()
        self._massive_connect_lock = asyncio.Lock()
        self._massive_authed: bool = False
        # FIX-05B.3-B2-A §C — symbols currently "joined" to the shared
        # connection (derived FROM the pre-existing chart-subscriber/
        # open-position lifecycle below, never a second business
        # registry — see _massive_register_symbol/_massive_unregister_
        # symbol). self._massive_shared_task is the ONE reader task
        # (async for raw in ws); self._massive_connection_watchdog_task
        # is the ONE per-connection staleness watchdog. Both None
        # whenever _massive_active_symbols is empty (Design Lock §0.B —
        # zero-active shutdown tears both down, never leaves a zombie).
        self._massive_active_symbols: set[str] = set()
        self._massive_shared_task: "asyncio.Task | None" = None
        self._massive_connection_watchdog_task: "asyncio.Task | None" = None
        # FIX-05B.3-B2-A §0.A / B2-FOREX-PROVIDER-CLEANUP-01 §3 —
        # consecutive stale-incident counter per symbol (0 = fresh/
        # never-stale-yet). 1 = first consecutive staleness, a
        # resubscribe was attempted. Reaching
        # _MASSIVE_SYMBOL_STALE_ESCALATION_THRESHOLD (and beyond) no
        # longer changes the ACTION taken — every incident triggers the
        # same resubscribe — only the log severity, as an observability
        # signal that a symbol is struggling to recover. Never escalates
        # to a second provider. Reset to 0 by ANY valid quote for that
        # symbol, and removed entirely on unregister.
        self._massive_symbol_stale_attempts: dict[str, int] = {}
        # FIX-05B.3-B1.1/B2 — wall-clock (time.monotonic()) of the last
        # GENUINE "ev":"C" quote the shared reader parsed and broadcast
        # for EACH symbol — set to "now" once more whenever that symbol
        # (re)joins an established connection or a fresh connection
        # (re)subscribes it (a grace window for its next quote), and
        # ONLY ever advanced further by an actual valid quote for THAT
        # symbol. Never touched by auth/status/malformed/unknown
        # messages, and a tick for one symbol never touches another's
        # entry. Read by _massive_connection_staleness_watchdog() to
        # detect "socket alive, data silent" per symbol AND connection-
        # wide — a failure mode connection-error handling alone never
        # catches (GOLDEN-MASSIVE-PRICECACHE-01).
        self._massive_last_quote_at: dict[str, float] = {}
        # GOLDEN-MARKETDATA-CRYPTO-01 — Massive Crypto shared-connection
        # state. Fully parallel to the _massive_* Forex block above (same
        # roles, same invariants, same _massive_connect_lock-equivalent
        # serialization) — deliberately NOT shared with it: a different
        # physical WS cluster (wss://.../crypto vs .../forex), a different
        # active-symbol set, and Option A (Design Lock §H) explicitly
        # avoids merging the two to keep Forex regression risk at zero.
        self._massive_crypto_ws = None
        self._massive_crypto_subscribed: set[str] = set()
        self._massive_crypto_connect_lock = asyncio.Lock()
        self._massive_crypto_authed: bool = False
        self._massive_crypto_active_symbols: set[str] = set()
        self._massive_crypto_shared_task: "asyncio.Task | None" = None
        self._massive_crypto_connection_watchdog_task: "asyncio.Task | None" = None
        self._massive_crypto_symbol_stale_attempts: dict[str, int] = {}
        self._massive_crypto_last_quote_at: dict[str, float] = {}
        # CRYPTO-QUOTE-DEDUP-01 — last (bid, ask) actually broadcast (full
        # _broadcast(), not _update_price_state()) per symbol. An exact
        # repeat of both against this is "no new price information for
        # the chart" — _massive_crypto_shared_loop() still refreshes
        # freshness/cache for it (_update_price_state()) but skips the
        # group_send. Crypto-only, never read by the Forex path. Reset on
        # every reconnect and popped on unregister — see
        # _massive_crypto_shared_loop()/_massive_crypto_unregister_symbol().
        self._massive_crypto_last_broadcast_quote: dict[str, tuple[float, float]] = {}
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

    async def fetch_massive_history_first_page(
        self, symbol: str, interval: str = "1m", limit: int = 200
    ) -> "tuple[list, _MassiveHistoryCursor | None]":
        """
        CHART-HISTORY-INSTANT-LOAD-01 — first-paint split of
        fetch_massive_history(). Makes exactly ONE REST call (page 1,
        sort=desc — newest bars first) and returns immediately:
        (bars_asc, cursor). `cursor` is None whenever there is nothing
        more to fetch — missing key/symbol/timeframe, a request failure,
        page 1 already reaching `limit`, or Massive reporting no
        next_url — in every one of those cases the returned bars ARE the
        final answer (byte-identical to what fetch_massive_history()
        would have produced) and the caller must never schedule a second
        call. Otherwise `cursor` carries everything fetch_massive_
        history_remaining() needs to continue from page 2 onward. Same
        fail-closed contract as fetch_massive_history(): a failure on
        page 1 returns ([], None), never raises.
        """
        if not MASSIVE_API_KEY:
            log.debug("[massive] no MASSIVE_API_KEY configured — skipping %s %s", symbol, interval)
            return [], None
        mapped = _massive_sym(symbol)
        tf = _MASSIVE_TF.get(interval)
        if mapped is None or tf is None:
            return [], None
        multiplier, timespan = tf
        date_range = _massive_range(interval, limit)
        if date_range is None:
            return [], None
        from_date, to_date = date_range

        loop = asyncio.get_event_loop()

        def _fetch(url: str) -> bytes:
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {MASSIVE_API_KEY}",
                    "User-Agent": "trx-sim/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.read()

        url = (
            f"https://api.massive.com/v2/aggs/ticker/{mapped}/range/"
            f"{multiplier}/{timespan}/{from_date}/{to_date}?limit={limit}&sort=desc"
        )

        try:
            raw = await loop.run_in_executor(None, _fetch, url)
            data = json.loads(raw)
        except Exception as exc:
            # Timeout/URLError/HTTPError(401/403/429/5xx)/JSON decode —
            # all transient/config failures, never response bodies or
            # the Authorization header. exc's repr never contains
            # MASSIVE_API_KEY (it's a header value, not part of the
            # URL/exception message urllib builds).
            log.debug("[massive] fetch failed for %s %s (page 1): %r", symbol, interval, exc)
            return [], None

        if data.get("status") != "OK":
            log.warning("[massive] non-OK status for %s %s: %r", symbol, interval, data.get("status"))
            return [], None

        page_bars: list = []
        for row in (data.get("results") or []):
            try:
                page_bars.append({
                    "time":   int(row["t"]) // 1000,
                    "open":   float(row["o"]),
                    "high":   float(row["h"]),
                    "low":    float(row["l"]),
                    "close":  float(row["c"]),
                    "volume": float(row.get("v", 0.0)),
                })
            except (KeyError, TypeError, ValueError):
                continue  # malformed row — skip it, never abort the batch

        next_url = data.get("next_url")
        cursor = None
        if len(page_bars) < limit and next_url:
            cursor = _MassiveHistoryCursor(
                symbol=symbol, interval=interval, limit=limit,
                bars_so_far=page_bars, pages_so_far=1, next_url=next_url,
            )

        result = _finalize_massive_bars(page_bars, limit)
        log.info("[feed] Massive %s %s — %d bars (1 page(s), first-page)",
                  symbol, interval, len(result))
        return result, cursor

    async def fetch_massive_history_remaining(self, cursor: "_MassiveHistoryCursor") -> list:
        """
        CHART-HISTORY-INSTANT-LOAD-01 — continues pagination from
        `cursor` (produced by fetch_massive_history_first_page()) through
        page 2 onward, up to _MASSIVE_FOREX_MAX_PAGES total pages.
        Returns the FINAL merged/deduped/trimmed/sorted-ascending result
        — same contract as fetch_massive_history() itself. Callers must
        never invoke this with cursor=None (fetch_massive_history_first_
        page() already returned the final answer in that case).
        """
        symbol, interval, limit = cursor.symbol, cursor.interval, cursor.limit
        loop = asyncio.get_event_loop()

        def _fetch(url: str) -> bytes:
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {MASSIVE_API_KEY}",
                    "User-Agent": "trx-sim/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.read()

        all_bars: list = list(cursor.bars_so_far)  # DESC arrival order
        pages = cursor.pages_so_far
        url = cursor.next_url

        while url and pages < _MASSIVE_FOREX_MAX_PAGES:
            try:
                raw = await loop.run_in_executor(None, _fetch, url)
                data = json.loads(raw)
            except Exception as exc:
                log.debug("[massive] fetch failed for %s %s (page %d): %r",
                          symbol, interval, pages + 1, exc)
                break
            pages += 1

            if data.get("status") != "OK":
                log.warning("[massive] non-OK status for %s %s: %r",
                            symbol, interval, data.get("status"))
                break

            for row in (data.get("results") or []):
                try:
                    all_bars.append({
                        "time":   int(row["t"]) // 1000,
                        "open":   float(row["o"]),
                        "high":   float(row["h"]),
                        "low":    float(row["l"]),
                        "close":  float(row["c"]),
                        "volume": float(row.get("v", 0.0)),
                    })
                except (KeyError, TypeError, ValueError):
                    continue  # malformed row — skip it, never abort the batch

            next_url = data.get("next_url")
            if len(all_bars) >= limit or not next_url:
                break
            url = next_url  # re-fetched via the SAME _fetch() -> same Authorization header

        result = _finalize_massive_bars(all_bars, limit)
        log.info("[feed] Massive %s %s — %d bars (%d page(s), depth-complete)",
                  symbol, interval, len(result), pages)
        return result

    async def fetch_massive_history(
        self, symbol: str, interval: str = "1m", limit: int = 200
    ) -> list:
        """
        FIX-05B.2-B/C — Massive REST historical aggregates, Forex ONLY
        (see _MASSIVE_ENABLED_SYMBOLS). Analogous to fetch_kline_history()
        above: async, run_in_executor + urllib.request, same {"time"
        (seconds), "open","high","low","close","volume"} output contract.
        Never raises for a normal provider failure (missing key,
        unsupported symbol/timeframe, HTTP 401/403/429/5xx, timeout,
        invalid JSON, status!=OK, missing/empty results, a malformed row)
        — every one of those returns [] so generate_history() (consumers.py)
        can fail-closed to history_unavailable exactly like the crypto path
        already does. results=[] is a legitimate, confirmed-real outcome
        (FIX-05B.2-A.1 — EUR/USD 1h can return HTTP 200/status=OK/
        resultsCount=0), never logged as a warning/error.

        Closed-candle filtering is NOT done here — _closed_only()
        (consumers.py::generate_history(), reusing market_data.feeds'
        own _is_closed()/_closed_only()) remains the single authority,
        exactly as it already is for the crypto path — this fetcher
        returns every bar Massive gave it, still-forming one included.

        CHART-GLOBAL-REGRESSION-01 — ACCEPTANCE FIX (freshness). Confirmed
        live: Massive's aggs endpoint returns only a small, timeframe-
        driven page size regardless of `limit` (identical across all 4
        Forex pairs) — the original ASC + _MASSIVE_MAX_PAGES=3 silently
        truncated 15m/1h history to 3-16 days stale, for every pair,
        never caught before because only 1m (which happens to fit in one
        page) had ever been checked against real data. Fixed the same way
        as fetch_massive_crypto_history(): request `sort=desc` (page 1
        always newest-first), paginate up to _MASSIVE_FOREX_MAX_PAGES,
        trim to the most recent `limit` bars, then sort ascending — same
        final contract as before. Degrades gracefully: a sparse timeframe
        (1h) may not reach the full requested depth within the cap, but
        the newest bar is always fresh — preferring less depth over
        stale-but-"complete" data.

        CHART-HISTORY-INSTANT-LOAD-01 — this is now a thin composition of
        fetch_massive_history_first_page() + fetch_massive_history_
        remaining(), kept for 100% backward compatibility with every
        existing caller/test that wants the complete answer in one await
        (byte-identical output to before this split existed).
        consumers.py's own history call-sites use the two split methods
        directly (first page awaited inline for a fast first paint,
        remaining pages run in a detached asyncio.Task) — see
        generate_history_first_page()/_complete_history_depth() there.
        """
        first, cursor = await self.fetch_massive_history_first_page(symbol, interval, limit)
        if cursor is None:
            return first
        return await self.fetch_massive_history_remaining(cursor)

    async def fetch_massive_crypto_history_first_page(
        self, symbol: str, interval: str = "1m", limit: int = 200
    ) -> "tuple[list, _MassiveHistoryCursor | None]":
        """
        CHART-HISTORY-INSTANT-LOAD-01 — first-paint split of
        fetch_massive_crypto_history(). Same contract as fetch_massive_
        history_first_page() (Forex) — see that docstring — mirrored here
        rather than shared (Design Lock Option A: duplicate, don't share
        across the Forex/Crypto boundary for the actual fetch logic;
        _finalize_massive_bars() is the one deliberate, provider-agnostic
        exception — see its own docstring)."""
        if not MASSIVE_API_KEY:
            log.debug("[massive-crypto] no MASSIVE_API_KEY configured — skipping %s %s", symbol, interval)
            return [], None
        mapped = _massive_crypto_sym(symbol)
        tf = _MASSIVE_TF.get(interval)
        if mapped is None or tf is None:
            return [], None
        multiplier, timespan = tf
        date_range = _massive_range(interval, limit)
        if date_range is None:
            return [], None
        from_date, to_date = date_range

        loop = asyncio.get_event_loop()

        def _fetch(url: str) -> bytes:
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {MASSIVE_API_KEY}",
                    "User-Agent": "trx-sim/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.read()

        url = (
            f"https://api.massive.com/v2/aggs/ticker/{mapped}/range/"
            f"{multiplier}/{timespan}/{from_date}/{to_date}?limit={limit}&sort=desc"
        )

        try:
            raw = await loop.run_in_executor(None, _fetch, url)
            data = json.loads(raw)
        except Exception as exc:
            log.debug("[massive-crypto] fetch failed for %s %s (page 1): %r", symbol, interval, exc)
            return [], None

        if data.get("status") != "OK":
            log.warning("[massive-crypto] non-OK status for %s %s: %r", symbol, interval, data.get("status"))
            return [], None

        page_bars: list = []
        for row in (data.get("results") or []):
            try:
                page_bars.append({
                    "time":   int(row["t"]) // 1000,
                    "open":   float(row["o"]),
                    "high":   float(row["h"]),
                    "low":    float(row["l"]),
                    "close":  float(row["c"]),
                    "volume": float(row.get("v", 0.0)),
                })
            except (KeyError, TypeError, ValueError):
                continue  # malformed row — skip it, never abort the batch

        next_url = data.get("next_url")
        cursor = None
        if len(page_bars) < limit and next_url:
            cursor = _MassiveHistoryCursor(
                symbol=symbol, interval=interval, limit=limit,
                bars_so_far=page_bars, pages_so_far=1, next_url=next_url,
            )

        result = _finalize_massive_bars(page_bars, limit)
        log.info("[feed] Massive-crypto %s %s — %d bars (1 page(s), first-page)",
                  symbol, interval, len(result))
        return result, cursor

    async def fetch_massive_crypto_history_remaining(self, cursor: "_MassiveHistoryCursor") -> list:
        """CHART-HISTORY-INSTANT-LOAD-01 — see fetch_massive_history_
        remaining()'s docstring (Forex); identical contract, mirrored for
        crypto's own _MASSIVE_CRYPTO_MAX_PAGES cap."""
        symbol, interval, limit = cursor.symbol, cursor.interval, cursor.limit
        loop = asyncio.get_event_loop()

        def _fetch(url: str) -> bytes:
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {MASSIVE_API_KEY}",
                    "User-Agent": "trx-sim/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.read()

        all_bars: list = list(cursor.bars_so_far)  # DESC arrival order
        pages = cursor.pages_so_far
        url = cursor.next_url

        while url and pages < _MASSIVE_CRYPTO_MAX_PAGES:
            try:
                raw = await loop.run_in_executor(None, _fetch, url)
                data = json.loads(raw)
            except Exception as exc:
                log.debug("[massive-crypto] fetch failed for %s %s (page %d): %r",
                          symbol, interval, pages + 1, exc)
                break
            pages += 1

            if data.get("status") != "OK":
                log.warning("[massive-crypto] non-OK status for %s %s: %r",
                            symbol, interval, data.get("status"))
                break

            for row in (data.get("results") or []):
                try:
                    all_bars.append({
                        "time":   int(row["t"]) // 1000,
                        "open":   float(row["o"]),
                        "high":   float(row["h"]),
                        "low":    float(row["l"]),
                        "close":  float(row["c"]),
                        "volume": float(row.get("v", 0.0)),
                    })
                except (KeyError, TypeError, ValueError):
                    continue  # malformed row — skip it, never abort the batch

            next_url = data.get("next_url")
            if len(all_bars) >= limit or not next_url:
                break
            url = next_url  # re-fetched via the SAME _fetch() -> same Authorization header

        result = _finalize_massive_bars(all_bars, limit)
        log.info("[feed] Massive-crypto %s %s — %d bars (%d page(s), depth-complete)",
                  symbol, interval, len(result), pages)
        return result

    async def fetch_massive_crypto_history(
        self, symbol: str, interval: str = "1m", limit: int = 200
    ) -> list:
        """
        GOLDEN-MARKETDATA-CRYPTO-01 — Massive REST historical aggregates,
        Crypto ONLY (see _MASSIVE_CRYPTO_ENABLED_SYMBOLS). A deliberate
        near-duplicate of fetch_massive_history() above (Design Lock §D /
        Option A — Forex stays untouched, crypto gets its own sibling
        rather than a shared/refactored code path): same aggs endpoint
        shape, same _MASSIVE_TF/_massive_range() (both fully generic,
        reused unchanged), same fail-closed contract ([] for any provider
        failure or unsupported symbol/timeframe — never raises, never
        fabricates a bar) — only the ticker resolution (_massive_crypto_
        sym() instead of _massive_sym()) and the pagination direction
        differ.

        ACCEPTANCE FIX (confirmed live against the real API) — Massive's
        crypto aggs endpoint returns only ~13 results per page regardless
        of `limit`, so reusing Forex's _MASSIVE_MAX_PAGES=3 (Forex's own
        endpoint DOES return up to the full `limit` in one page) silently
        truncated history ~4 days short of "now". Fixed by requesting
        `sort=desc` (newest-first — confirmed live: page 1 always
        contains the newest bars, regardless of how many pages follow)
        and paginating up to _MASSIVE_CRYPTO_MAX_PAGES=20, then trimming
        to the most recent `limit` bars before the final chronological
        sort. Degrades gracefully: a sparse timeframe (e.g. 1h) may not
        reach the full requested depth within 20 pages, but the newest
        bar is always fresh — preferring less depth over stale-but-
        "complete" data (Design Lock: explicit, authorized tradeoff).

        CHART-HISTORY-INSTANT-LOAD-01 — now a thin composition of
        fetch_massive_crypto_history_first_page() + fetch_massive_crypto_
        history_remaining(), kept for 100% backward compatibility (see
        fetch_massive_history()'s own docstring for the full rationale).
        """
        first, cursor = await self.fetch_massive_crypto_history_first_page(symbol, interval, limit)
        if cursor is None:
            return first
        return await self.fetch_massive_crypto_history_remaining(cursor)

    async def _update_price_state(self, symbol: str, bid: float, ask: float,
                                   source: str = "live") -> float:
        """
        CRYPTO-QUOTE-DEDUP-01 — the freshness/cache half of _broadcast(),
        extracted verbatim (same statements, same order) so _broadcast()
        itself is a pure composition of this plus its own group_send —
        zero behavior change for any existing _broadcast() caller.

        Updates everything has_price()/get_validated_quote() read
        (_bids/_asks/_prices/_price_ts/_price_source, all under
        self._lock), refreshes the Redis price cache (same TTL, same
        _write_price_cache() call), and records the observability
        "connection is alive" tick — but NEVER touches channel_layer.
        Deliberately callable on its own for a quote that carries no new
        price information (see _massive_crypto_shared_loop()): freshness
        and "a real price change happened" are two separate concepts —
        a duplicate quote is still fresh, it just isn't new.

        Returns the rounded mid, so a full _broadcast() doesn't need to
        recompute it for its group_send payload.
        """
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
        # GOLDEN-WEEKEND-RISK-01 — durable last-known-good price, throttled
        # to at most once every _DURABLE_PRICE_WRITE_INTERVAL_SECS per
        # symbol (never one DB write per tick). Only accepted sources
        # persist; "sim" and anything else are silently skipped here too
        # (defense in depth — _write_durable_price_sync() re-checks anyway).
        if source in _DURABLE_PRICE_VALID_SOURCES:
            _now_mono = time.time()
            _last_write = self._last_durable_write_ts.get(symbol, 0.0)
            if _now_mono - _last_write >= _DURABLE_PRICE_WRITE_INTERVAL_SECS:
                self._last_durable_write_ts[symbol] = _now_mono
                await _write_durable_price(symbol, bid, ask, mid, source)
        # FOUNDATION-13 — records only a timestamp (no bid/ask/mid) in the
        # observability store, gated by MARKET_DATA_OBSERVABILITY_ENABLED.
        if self._observability_enabled():
            try:
                from market_data.observability import record_tick
                record_tick(symbol)
            except Exception as exc:
                log.debug("[observability] tick recording failed for %s (non-fatal): %r", symbol, exc)
        return mid

    async def _broadcast(self, symbol: str, cl, bid: float, ask: float, ts: int,
                          source: str = "live") -> None:
        mid = await self._update_price_state(symbol, bid, ask, source)
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

    async def _broadcast_trade(self, symbol: str, cl, price: float, size: float, ts: int) -> None:
        """
        MASSIVE-CRYPTO-TRADE-CANDLES-01 — the SOLE broadcast point for a
        Massive crypto trade (ev="XT"). Deliberately never calls
        _broadcast() or _update_price_state(): a trade price is NEVER
        execution authority — no Redis price-cache write, no _bids/
        _asks/_price_ts/_price_source mutation, no interaction with the
        quote dedup state (_massive_crypto_last_broadcast_quote,
        CRYPTO-QUOTE-DEDUP-01) or anything has_price()/get_validated_
        quote() read. Its only job is to hand the trade to every
        connected TradingConsumer for that consumer's own candle
        aggregation (each keeps its own per-timeframe bucket — see
        consumers.py::price_trade()) — chart/candle/volume only, never a
        bid/ask/mid, never "massive" quote source semantics.
        """
        await cl.group_send(
            self.group_for(symbol),
            {
                "type":   "price.trade",
                "symbol": symbol,
                "price":  price,
                "size":   size,
                "time":   ts,
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

        # B2-FOREX-PROVIDER-CLEANUP-01 §7 — regression guard. Massive is
        # the sole runtime provider for these 4 active Forex pairs.
        # This router's own dispatch table below has no "massive" entry
        # — the dormant InstrumentProfile bridge layer this router path
        # ultimately draws its provider mappings from never builds one
        # either — so if MARKET_DATA_ROUTER_ENABLED is ever turned on for
        # one of these symbols, a decision could otherwise silently hand
        # it to Finnhub. Fail closed instead: raise, which the caller
        # (_try_live) catches and falls back to _try_live_legacy — which
        # itself never runs Finnhub for these 4 symbols either.
        if provider_id == "finnhub" and symbol in _MASSIVE_WS_ENABLED_SYMBOLS:
            raise RuntimeError(
                f"router_selected_finnhub_for_massive_forex_symbol symbol={symbol!r} "
                "— refusing (Finnhub is not a valid Forex fallback)"
            )

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
        Original _try_live logic — Massive (for the 2
        _MASSIVE_CRYPTO_ENABLED_SYMBOLS, exclusively, GOLDEN-MARKETDATA-
        CRYPTO-01) -> Binance -> Kraken -> Massive (for the 4
        _MASSIVE_WS_ENABLED_SYMBOLS, exclusively — no Finnhub fallback,
        B2-FOREX-PROVIDER-CLEANUP-01) -> Finnhub (every other "/" symbol).
        This is what every symbol runs when the router flag is off, and
        what any symbol runs when it's not on the allowlist, and what an
        allowlisted symbol falls back to on any router-path failure.
        """
        # GOLDEN-MARKETDATA-CRYPTO-01 — Massive is the SOLE runtime
        # provider for BTCUSD/ETHUSD, checked BEFORE Binance/Kraken so
        # those branches become functionally unreachable for these 2
        # symbols (their code stays in place, unmodified, for every other
        # crypto symbol — none exist today, GOLDEN-MARKETDATA-CRYPTO-01
        # §I "KEEP AS DORMANT"). Same no-fallback contract as the Forex
        # Massive branch below: a real failure here means the symbol goes
        # unpriced rather than a silent handoff to Binance/Kraken.
        if MASSIVE_API_KEY and websockets and symbol in _MASSIVE_CRYPTO_ENABLED_SYMBOLS:
            try:
                await self._massive_crypto_loop(symbol, channel_layer)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("[feed] Massive Crypto failed for %s (%r)", symbol, exc)
            return False

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

        # FIX-05B.3-B1/B2/B2-FOREX-PROVIDER-CLEANUP-01 — Massive is the
        # SOLE runtime provider for all 4 _MASSIVE_WS_ENABLED_SYMBOLS,
        # all sharing ONE Massive connection (see _massive_shared_loop).
        # _massive_forex_loop(symbol,...) is a thin per-symbol adapter
        # that registers with the shared connection and blocks until
        # cancelled (normal unsubscribe/close, lifecycle unchanged) —
        # persistent per-symbol staleness is handled entirely inside the
        # connection watchdog (resubscribe, escalating log severity)
        # without ever raising here. There is deliberately no fallback
        # for these 4 symbols: a real, unexpected failure here means the
        # symbol goes unpriced (get_validated_quote() already rejects
        # stale/missing data for financial decisions) rather than a
        # silent handoff to a second provider.
        if MASSIVE_API_KEY and websockets and symbol in _MASSIVE_WS_ENABLED_SYMBOLS:
            try:
                await self._massive_forex_loop(symbol, channel_layer)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("[feed] Massive Forex failed for %s (%r)", symbol, exc)
            return False

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

    async def _massive_register_symbol(self, symbol: str, channel_layer) -> None:
        """
        FIX-05B.3-B2-A §4/§10 — called once per _massive_forex_loop(symbol,...)
        invocation. Adds *symbol* to the shared connection's active set,
        starts the ONE shared connection if this is the first active
        symbol process-wide, or sends an incremental subscribe if the
        connection is already live and authed. Never a second
        connection, never a second reader — self._massive_shared_task is
        checked/created under the lock with no `await` between the check
        and the create() call (same race-free idiom already used by
        _ensure_running()/ensure_position_feed_reconciliation_started()
        elsewhere in this file — ONE asyncio.create_task() call is
        synchronous/atomic on a single-threaded event loop).
        """
        async with self._massive_connect_lock:
            self._massive_symbol_stale_attempts[symbol] = 0
            self._massive_active_symbols.add(symbol)
            self._massive_last_quote_at.setdefault(symbol, time.monotonic())

            if self._massive_shared_task is None or self._massive_shared_task.done():
                self._massive_shared_task = asyncio.create_task(
                    self._massive_shared_loop(channel_layer)
                )
            elif (
                self._massive_ws is not None and self._massive_authed
                and symbol not in self._massive_subscribed
            ):
                # Connection already live and authed — join directly with
                # an incremental subscribe. If the connection exists but
                # is not yet authed, _massive_shared_loop's own
                # auth-success handler subscribes every symbol currently
                # in self._massive_active_symbols (including this one) —
                # no extra action needed here in that case.
                try:
                    await self._massive_ws.send(json.dumps({
                        "action": "subscribe", "params": _massive_ws_param(symbol),
                    }))
                    self._massive_subscribed.add(symbol)
                except Exception as exc:
                    log.debug(
                        "[feed] massive incremental subscribe failed for %s "
                        "(non-fatal, next reconnect resubscribes it): %r",
                        symbol, exc,
                    )

    async def _massive_unregister_symbol(self, symbol: str) -> None:
        """
        FIX-05B.3-B2-A §4/§0.B — inverse of _massive_register_symbol().
        Removes *symbol* from the shared connection's bookkeeping, sends
        an unsubscribe if the connection is still live and authed, and —
        if this was the LAST active symbol — tears down the entire
        shared service (cancels + awaits the reader task, which in its
        own `finally` cancels + awaits its connection watchdog) and
        clears every piece of connection-level state, all under the SAME
        lock acquisition held for this whole method. That means a
        concurrent _massive_register_symbol() call for a new symbol
        cannot observe a half-torn-down service: it either sees the old
        service still fully alive, or waits for this teardown to fully
        finish and then correctly starts exactly one brand-new one —
        never joins a zombie (Design Lock §0.B/§11).

        Cancelling self._massive_shared_task while holding this same
        lock never risks deadlock: cancellation only requires the target
        task to eventually RELEASE the lock if it currently holds it
        (unwinding a `async with` always does that cleanly), never to
        acquire it — and if the target is currently waiting to acquire
        this same lock, asyncio.Lock.acquire() is itself a safe
        cancellation point.
        """
        async with self._massive_connect_lock:
            self._massive_active_symbols.discard(symbol)
            self._massive_symbol_stale_attempts.pop(symbol, None)
            self._massive_last_quote_at.pop(symbol, None)
            was_subscribed = symbol in self._massive_subscribed
            self._massive_subscribed.discard(symbol)
            if was_subscribed and self._massive_ws is not None and self._massive_authed:
                try:
                    await self._massive_ws.send(json.dumps({
                        "action": "unsubscribe", "params": _massive_ws_param(symbol),
                    }))
                except Exception as exc:
                    log.debug(
                        "[feed] massive unsubscribe failed for %s "
                        "(non-fatal, connection is tearing down or will reconnect): %r",
                        symbol, exc,
                    )

            if not self._massive_active_symbols and self._massive_shared_task is not None:
                shared_task = self._massive_shared_task
                shared_task.cancel()
                try:
                    await shared_task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    log.debug("[feed] massive shared service teardown raised (non-fatal): %r", exc)
                self._massive_shared_task = None
                self._massive_connection_watchdog_task = None
                self._massive_ws = None
                self._massive_authed = False
                self._massive_subscribed.clear()
                log.info("[feed] Massive shared connection torn down (zero active symbols)")

    async def _massive_forex_loop(
        self, symbol: str, channel_layer, *,
        on_first_tick: Optional[Callable[[], None]] = None,
        on_terminal_failure: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        """
        FIX-05B.3-B2-A / B2-FOREX-PROVIDER-CLEANUP-01 §4 — thin
        per-symbol adapter onto the ONE shared Massive connection
        (_massive_shared_loop below). B1's one-connection-per-symbol
        behavior is gone: this never calls websockets.connect() itself
        anymore. Returns immediately, without registering anything, for
        any symbol outside _MASSIVE_WS_ENABLED_SYMBOLS.

        Blocks for as long as the shared connection is registered to
        serve *symbol* — there is only ONE way this method ends now:
        cancelled from outside (panel closed / no more open position —
        the SAME existing per-symbol task lifecycle as every other
        provider loop, entirely unchanged), which `finally` always
        unregisters cleanly on its way out. Massive never gives up on a
        registered symbol and never hands it to a second provider —
        persistent per-symbol staleness is handled entirely inside the
        connection watchdog (resubscribe, escalating log severity)
        without ever waking this method up.

        on_first_tick/on_terminal_failure: unused here, same as B1 —
        kept only for signature symmetry with the other provider loops'
        shared contract (FOUNDATION-10); _try_live_legacy never passes
        them.
        """
        if symbol not in _MASSIVE_WS_ENABLED_SYMBOLS:
            return
        await self._massive_register_symbol(symbol, channel_layer)
        try:
            # Blocks until this task is cancelled — nothing else ever
            # completes this Event, by design (§3/§4).
            await asyncio.Event().wait()
        finally:
            await self._massive_unregister_symbol(symbol)

    async def _massive_connection_staleness_watchdog(self, ws, state: dict) -> None:
        """
        FIX-05B.3-B2-A §6/§8/§9 — the ONE watchdog for the shared
        connection (created by _massive_shared_loop right after auth is
        sent, cancelled in that same method's `finally` the instant this
        connection attempt ends, for ANY reason — mirrors B1's
        per-connection watchdog lifecycle exactly, just now covering
        every active symbol on ONE connection instead of a single
        symbol on its own connection).

        Polls every _MASSIVE_WATCHDOG_POLL_SECONDS. For every symbol
        currently in self._massive_active_symbols, compares wall-clock
        (time.monotonic()) against self._massive_last_quote_at[symbol]
        — updated ONLY by the reader's own "ev":"C" parsing branch,
        never by auth/status/malformed/unknown messages, and never by
        this watchdog itself except as a grace-window reset after a
        resubscribe (see below).

        Two distinct outcomes, never confused:

        CONNECTION STALE (§9) — every active symbol is individually
        stale (no valid quote for ANY of them in > DATA_STALE_TIMEOUT):
        logs once, records state["reason"]="connection_stale", resets
        every active symbol's stale_attempts to 0 (a fresh reconnect
        deserves a fresh consecutive-incident count), and closes `ws`.
        The whole connection is torn down and reconnected by
        _massive_shared_loop's existing backoff/reconnect machinery —
        same as B1, now re-subscribing every symbol still present in
        self._massive_active_symbols at reconnect time.

        SYMBOL STALE (§8) — one or more (but not ALL) active symbols are
        individually stale while at least one sibling stays fresh:
        NEVER closes the connection. Per stale symbol, increments
        self._massive_symbol_stale_attempts[symbol] and ALWAYS sends
        unsubscribe+subscribe for JUST that symbol (under the lock, only
        if the connection is authed), resetting that symbol's grace
        window (last_quote_at = now) — every single incident, without a
        cap (B2-FOREX-PROVIDER-CLEANUP-01 §3/§E). The only thing that
        changes once attempts reaches
        _MASSIVE_SYMBOL_STALE_ESCALATION_THRESHOLD is the log severity
        (WARNING -> ERROR), purely for operational visibility that a
        symbol is struggling to recover — there is no second provider to
        hand it to, no event to set, and the per-symbol adapter
        (_massive_forex_loop) is never woken by this watchdog. Massive
        keeps retrying that symbol indefinitely, exactly like the
        connection-level reconnect already does.

        Never touches Redis/_broadcast()/the DB/the frontend directly —
        closing the socket or resubscribing a symbol are its ONLY side
        effects; the existing reconnect/backoff machinery
        (_massive_shared_loop) does everything else (Design Lock §6/§7 —
        no second reconnect mechanism, no fallback mechanism at all).
        """
        try:
            while True:
                await asyncio.sleep(_MASSIVE_WATCHDOG_POLL_SECONDS)
                active = set(self._massive_active_symbols)
                if not active:
                    continue
                now = time.monotonic()
                stale = [
                    s for s in active
                    if (now - self._massive_last_quote_at.get(s, now)) > DATA_STALE_TIMEOUT
                ]
                if not stale:
                    continue

                if len(stale) == len(active):
                    oldest = min(self._massive_last_quote_at.get(s, now) for s in active)
                    log.warning(
                        "[feed] Massive connection stale — no valid quote for ANY of %s in %.1fs",
                        sorted(active), now - oldest,
                    )
                    state["reason"] = "connection_stale"
                    for s in active:
                        self._massive_symbol_stale_attempts[s] = 0
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    return

                for sym in stale:
                    attempts = self._massive_symbol_stale_attempts.get(sym, 0) + 1
                    self._massive_symbol_stale_attempts[sym] = attempts
                    if attempts >= _MASSIVE_SYMBOL_STALE_ESCALATION_THRESHOLD:
                        log.error(
                            "[feed] Massive %s stale after %d consecutive incidents — "
                            "no fallback provider, resubscribing again",
                            sym, attempts,
                        )
                    else:
                        log.warning(
                            "[feed] Massive %s stale data — no valid quote for %.1fs (attempt %d/%d, resubscribing)",
                            sym, now - self._massive_last_quote_at.get(sym, now),
                            attempts, _MASSIVE_SYMBOL_STALE_ESCALATION_THRESHOLD,
                        )
                    try:
                        async with self._massive_connect_lock:
                            if self._massive_ws is not None and self._massive_authed:
                                param = _massive_ws_param(sym)
                                await self._massive_ws.send(json.dumps({"action": "unsubscribe", "params": param}))
                                await self._massive_ws.send(json.dumps({"action": "subscribe", "params": param}))
                            self._massive_last_quote_at[sym] = time.monotonic()
                    except Exception as exc:
                        log.debug("[feed] massive per-symbol resubscribe failed for %s (non-fatal): %r", sym, exc)
        except asyncio.CancelledError:
            raise

    async def _massive_shared_loop(self, channel_layer) -> None:
        """
        FIX-05B.3-B2-A §3/§6/§9 — the ONE Massive connection + ONE
        reader, process-wide, serving every symbol currently in
        self._massive_active_symbols concurrently. Started by
        _massive_register_symbol() the instant the first symbol joins;
        cancelled by _massive_unregister_symbol() the instant the last
        symbol leaves (Design Lock §0.B — never left running with zero
        active symbols, never a zombie).

        Auth-then-subscribe-batch: after an explicit auth_success, sends
        ONE subscribe covering every symbol currently active (read live
        under the lock at that moment, never a stale snapshot from
        connect time) — a symbol that joins later, while this connection
        is already authed, gets an incremental subscribe instead (see
        _massive_register_symbol) — never a second connection.

        Each "ev":"C" quote is routed by its own ev["p"] to the matching
        symbol's freshness timestamp + _broadcast() call — the SAME
        single choke point every other provider already uses, called
        exactly once per valid quote, keyed by that quote's own symbol
        (Design Lock §12 — no cross-symbol contamination: EUR/USD
        bid/ask can never reach GBP/USD, since routing is entirely by
        ev["p"], never by call order or shared mutable state). An event
        for a symbol not currently in self._massive_active_symbols
        (e.g. a straggler for a symbol that just unregistered) is
        dropped, never routed.

        Reconnects with exponential backoff (2/4/8/16/30s cap),
        INDEFINITELY — same policy as B1, now covering all active
        symbols together. A connection-staleness reconnect
        (watchdog-triggered) skips the sleep and resets backoff to its
        2s floor, exactly as B1 did — self-triggered closure, not a
        real failure signal.

        Every quote still passes through _validate_quote_values() (Capa
        A) before ever reaching _broadcast() — no new state, no new
        Redis write path, no new frontend contract. source="massive"
        needs no allowlist change anywhere (confirmed, FIX-05B.3-A §G).
        """
        backoff = 2.0
        while True:
            watchdog_state = {"reason": None}
            try:
                async with websockets.connect(
                    _MASSIVE_WS_URL,
                    ping_interval=20, ping_timeout=20,
                    close_timeout=10, max_queue=256,
                ) as ws:
                    async with self._massive_connect_lock:
                        self._massive_ws = ws
                        self._massive_authed = False
                        # Grace window for every symbol about to be
                        # (re)subscribed — same reasoning as B1, looped
                        # over the live active-symbol set. A fresh
                        # connection deserves a fresh consecutive-
                        # incident count too.
                        now = time.monotonic()
                        for sym in self._massive_active_symbols:
                            self._massive_last_quote_at[sym] = now
                            self._massive_symbol_stale_attempts[sym] = 0
                        await ws.send(json.dumps({"action": "auth", "params": MASSIVE_API_KEY}))

                    watchdog_task = asyncio.create_task(
                        self._massive_connection_staleness_watchdog(ws, watchdog_state)
                    )
                    self._massive_connection_watchdog_task = watchdog_task
                    try:
                        authed = False
                        async for raw in ws:
                            for ev in _parse_massive_events(raw):
                                ev_type = ev.get("ev")
                                if ev_type == "status":
                                    status = ev.get("status")
                                    if status == "auth_success":
                                        authed = True
                                        async with self._massive_connect_lock:
                                            self._massive_authed = True
                                            active_now = set(self._massive_active_symbols)
                                            if active_now:
                                                await ws.send(json.dumps({
                                                    "action": "subscribe",
                                                    "params": _massive_ws_params_batch(active_now),
                                                }))
                                                self._massive_subscribed |= active_now
                                    elif status == "error":
                                        raise RuntimeError(
                                            f"massive_auth_or_subscribe_error: {ev.get('message')}"
                                        )
                                    continue
                                if ev_type != "C" or not authed:
                                    # A quote before auth_success should never
                                    # happen (Massive doesn't stream pre-auth);
                                    # defensive only, never a real path.
                                    continue
                                sym = ev.get("p")
                                if sym not in self._massive_active_symbols:
                                    # Not (or no longer) an active symbol —
                                    # never routed, never broadcast.
                                    continue
                                try:
                                    bid = float(ev["b"])
                                    ask = float(ev["a"])
                                    ts  = int(ev["t"]) // 1000
                                except (KeyError, TypeError, ValueError):
                                    continue
                                if not _validate_quote_values(sym, bid, ask):
                                    continue
                                # FIX-05B.3-B2-A §7 — the ONLY place this
                                # symbol's timestamp advances past its
                                # grace-window seed value: a genuine,
                                # validated quote FOR THIS symbol. Never
                                # touches any other symbol's entry.
                                self._massive_last_quote_at[sym] = time.monotonic()
                                self._massive_symbol_stale_attempts[sym] = 0
                                await self._broadcast(sym, channel_layer, bid, ask, ts, source="massive")
                                backoff = 2.0
                    finally:
                        # Design Lock §10 — at most 1 connection watchdog
                        # per connection; always cancelled and awaited
                        # here, regardless of whether the read loop above
                        # exited via exception or normally, so no task is
                        # ever left orphaned.
                        watchdog_task.cancel()
                        try:
                            await watchdog_task
                        except asyncio.CancelledError:
                            pass
                        self._massive_connection_watchdog_task = None
                # Design Lock §6 — reached whenever the `async with
                # websockets.connect(...)` block above exits WITHOUT an
                # exception propagating past it: `async for raw in ws`
                # ended cleanly (StopAsyncIteration), which is exactly
                # what a watchdog-triggered ws.close() can produce. Never
                # treat this as "done" — always cycle back through the
                # same reconnect path a real error would.
                self._massive_subscribed.clear()
                self._massive_ws = None
                self._massive_authed = False
                if watchdog_state["reason"] == "connection_stale":
                    log.warning("[feed] Massive shared connection reconnecting after connection-wide staleness")
                    backoff = 2.0
                    continue
                log.warning("[feed] Massive shared connection ended — reconnect in %.0fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            except asyncio.CancelledError:
                self._massive_subscribed.clear()
                self._massive_ws = None
                self._massive_authed = False
                raise
            except Exception as exc:
                self._massive_subscribed.clear()
                self._massive_ws = None
                self._massive_authed = False
                if watchdog_state["reason"] == "connection_stale":
                    log.warning("[feed] Massive shared connection reconnecting after connection-wide staleness")
                    backoff = 2.0
                    continue
                log.error(
                    "[feed] Massive shared connection error: %r — reconnect in %.0fs",
                    exc, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    # ── GOLDEN-MARKETDATA-CRYPTO-01 — Massive Crypto shared connection ──
    # Deliberate structural mirror of the 5 _massive_* Forex methods above
    # (register/unregister/per-symbol adapter/watchdog/shared reader) —
    # same invariants, same lock-free-of-await create-task idiom, same
    # zero-active-symbols teardown discipline, same "always resubscribe,
    # never escalate to a second provider" watchdog policy (reusing
    # DATA_STALE_TIMEOUT/_MASSIVE_WATCHDOG_POLL_SECONDS/
    # _MASSIVE_SYMBOL_STALE_ESCALATION_THRESHOLD unchanged, Design Lock
    # §F) — built correctly from day one, never had the Finnhub-escalation
    # mistake B2-FOREX-PROVIDER-CLEANUP-01 had to remove. NOT shared code
    # with the Forex methods (Option A, Design Lock §H): a different
    # physical connection (_MASSIVE_WS_CRYPTO_URL), a different active-
    # symbol set, entirely separate state.

    async def _massive_crypto_register_symbol(self, symbol: str, channel_layer) -> None:
        """Adds *symbol* to the shared crypto connection's active set,
        starts the ONE shared connection if this is the first active
        symbol process-wide, or sends an incremental subscribe if the
        connection is already live and authed. Mirrors
        _massive_register_symbol() exactly."""
        async with self._massive_crypto_connect_lock:
            self._massive_crypto_symbol_stale_attempts[symbol] = 0
            self._massive_crypto_active_symbols.add(symbol)
            self._massive_crypto_last_quote_at.setdefault(symbol, time.monotonic())

            if self._massive_crypto_shared_task is None or self._massive_crypto_shared_task.done():
                self._massive_crypto_shared_task = asyncio.create_task(
                    self._massive_crypto_shared_loop(channel_layer)
                )
            elif (
                self._massive_crypto_ws is not None and self._massive_crypto_authed
                and symbol not in self._massive_crypto_subscribed
            ):
                param = _massive_crypto_ws_param(symbol)
                if param is None:
                    return
                try:
                    await self._massive_crypto_ws.send(json.dumps({
                        "action": "subscribe", "params": param,
                    }))
                    self._massive_crypto_subscribed.add(symbol)
                except Exception as exc:
                    log.debug(
                        "[feed] massive-crypto incremental subscribe failed for %s "
                        "(non-fatal, next reconnect resubscribes it): %r",
                        symbol, exc,
                    )

    async def _massive_crypto_unregister_symbol(self, symbol: str) -> None:
        """Inverse of _massive_crypto_register_symbol(). Mirrors
        _massive_unregister_symbol() exactly, including the same
        never-joins-a-zombie teardown-under-one-lock-acquisition
        guarantee."""
        async with self._massive_crypto_connect_lock:
            self._massive_crypto_active_symbols.discard(symbol)
            self._massive_crypto_symbol_stale_attempts.pop(symbol, None)
            self._massive_crypto_last_quote_at.pop(symbol, None)
            self._massive_crypto_last_broadcast_quote.pop(symbol, None)  # CRYPTO-QUOTE-DEDUP-01
            was_subscribed = symbol in self._massive_crypto_subscribed
            self._massive_crypto_subscribed.discard(symbol)
            if was_subscribed and self._massive_crypto_ws is not None and self._massive_crypto_authed:
                param = _massive_crypto_ws_param(symbol)
                if param is not None:
                    try:
                        await self._massive_crypto_ws.send(json.dumps({
                            "action": "unsubscribe", "params": param,
                        }))
                    except Exception as exc:
                        log.debug(
                            "[feed] massive-crypto unsubscribe failed for %s "
                            "(non-fatal, connection is tearing down or will reconnect): %r",
                            symbol, exc,
                        )

            if not self._massive_crypto_active_symbols and self._massive_crypto_shared_task is not None:
                shared_task = self._massive_crypto_shared_task
                shared_task.cancel()
                try:
                    await shared_task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    log.debug("[feed] massive-crypto shared service teardown raised (non-fatal): %r", exc)
                self._massive_crypto_shared_task = None
                self._massive_crypto_connection_watchdog_task = None
                self._massive_crypto_ws = None
                self._massive_crypto_authed = False
                self._massive_crypto_subscribed.clear()
                log.info("[feed] Massive crypto shared connection torn down (zero active symbols)")

    async def _massive_crypto_loop(
        self, symbol: str, channel_layer, *,
        on_first_tick: Optional[Callable[[], None]] = None,
        on_terminal_failure: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        """Thin per-symbol adapter onto the ONE shared Massive crypto
        connection (_massive_crypto_shared_loop below). Mirrors
        _massive_forex_loop() exactly — registers, blocks until cancelled,
        unregisters in `finally`. Never escalates to a second provider;
        persistent per-symbol staleness is handled entirely inside the
        connection watchdog. Returns immediately, without registering
        anything, for any symbol outside _MASSIVE_CRYPTO_ENABLED_SYMBOLS.

        on_first_tick/on_terminal_failure: unused here, same as the Forex
        adapter — kept only for signature symmetry with the other provider
        loops' shared contract (FOUNDATION-10); _try_live_legacy never
        passes them.
        """
        if symbol not in _MASSIVE_CRYPTO_ENABLED_SYMBOLS:
            return
        await self._massive_crypto_register_symbol(symbol, channel_layer)
        try:
            await asyncio.Event().wait()
        finally:
            await self._massive_crypto_unregister_symbol(symbol)

    async def _massive_crypto_connection_staleness_watchdog(self, ws, state: dict) -> None:
        """The ONE watchdog for the shared crypto connection. Mirrors
        _massive_connection_staleness_watchdog() exactly: connection-wide
        staleness (every active symbol individually stale) closes `ws` and
        lets _massive_crypto_shared_loop's reconnect machinery take over;
        per-symbol staleness (some but not all) NEVER closes the
        connection, always resubscribes just that symbol, and only steps
        up log severity (WARNING -> ERROR) at
        _MASSIVE_SYMBOL_STALE_ESCALATION_THRESHOLD — no fallback provider,
        no escalation event, ever."""
        try:
            while True:
                await asyncio.sleep(_MASSIVE_WATCHDOG_POLL_SECONDS)
                active = set(self._massive_crypto_active_symbols)
                if not active:
                    continue
                now = time.monotonic()
                stale = [
                    s for s in active
                    if (now - self._massive_crypto_last_quote_at.get(s, now)) > DATA_STALE_TIMEOUT
                ]
                if not stale:
                    continue

                if len(stale) == len(active):
                    oldest = min(self._massive_crypto_last_quote_at.get(s, now) for s in active)
                    log.warning(
                        "[feed] Massive crypto connection stale — no valid quote for ANY of %s in %.1fs",
                        sorted(active), now - oldest,
                    )
                    state["reason"] = "connection_stale"
                    for s in active:
                        self._massive_crypto_symbol_stale_attempts[s] = 0
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    return

                for sym in stale:
                    attempts = self._massive_crypto_symbol_stale_attempts.get(sym, 0) + 1
                    self._massive_crypto_symbol_stale_attempts[sym] = attempts
                    if attempts >= _MASSIVE_SYMBOL_STALE_ESCALATION_THRESHOLD:
                        log.error(
                            "[feed] Massive crypto %s stale after %d consecutive incidents — "
                            "no fallback provider, resubscribing again",
                            sym, attempts,
                        )
                    else:
                        log.warning(
                            "[feed] Massive crypto %s stale data — no valid quote for %.1fs (attempt %d/%d, resubscribing)",
                            sym, now - self._massive_crypto_last_quote_at.get(sym, now),
                            attempts, _MASSIVE_SYMBOL_STALE_ESCALATION_THRESHOLD,
                        )
                    param = _massive_crypto_ws_param(sym)
                    if param is None:
                        continue
                    try:
                        async with self._massive_crypto_connect_lock:
                            if self._massive_crypto_ws is not None and self._massive_crypto_authed:
                                await self._massive_crypto_ws.send(json.dumps({"action": "unsubscribe", "params": param}))
                                await self._massive_crypto_ws.send(json.dumps({"action": "subscribe", "params": param}))
                            self._massive_crypto_last_quote_at[sym] = time.monotonic()
                    except Exception as exc:
                        log.debug("[feed] massive-crypto per-symbol resubscribe failed for %s (non-fatal): %r", sym, exc)
        except asyncio.CancelledError:
            raise

    async def _massive_crypto_shared_loop(self, channel_layer) -> None:
        """The ONE Massive crypto connection + ONE reader, process-wide.
        Mirrors _massive_shared_loop() exactly, with two crypto-specific
        differences confirmed live in the GOLDEN-MARKETDATA-CRYPTO-01
        provider-access audit: the quote event type is "XQ" (not "C"),
        keyed by ev["pair"] (Massive's own hyphenated spelling, e.g.
        "BTC-USD") rather than ev["p"] (already-canonical for Forex) — so
        incoming events are mapped back to the canonical symbol via
        _massive_crypto_symbol_from_pair() before routing; an unmapped
        pair is dropped, never routed. Bid/ask keys are "bp"/"ap" (not
        Forex's "b"/"a")."""
        backoff = 2.0
        while True:
            watchdog_state = {"reason": None}
            try:
                async with websockets.connect(
                    _MASSIVE_WS_CRYPTO_URL,
                    ping_interval=20, ping_timeout=20,
                    close_timeout=10, max_queue=256,
                ) as ws:
                    async with self._massive_crypto_connect_lock:
                        self._massive_crypto_ws = ws
                        self._massive_crypto_authed = False
                        now = time.monotonic()
                        for sym in self._massive_crypto_active_symbols:
                            self._massive_crypto_last_quote_at[sym] = now
                            self._massive_crypto_symbol_stale_attempts[sym] = 0
                            # CRYPTO-QUOTE-DEDUP-01 — a fresh connection
                            # deserves a fresh first broadcast, even if
                            # the first quote it delivers happens to
                            # match whatever was last known before the
                            # disconnect: reconnecting is itself a signal
                            # worth re-confirming to consumers, and this
                            # guarantees the dedup state can never suppress
                            # every quote on a connection that just came up.
                            self._massive_crypto_last_broadcast_quote.pop(sym, None)
                        await ws.send(json.dumps({"action": "auth", "params": MASSIVE_API_KEY}))

                    watchdog_task = asyncio.create_task(
                        self._massive_crypto_connection_staleness_watchdog(ws, watchdog_state)
                    )
                    self._massive_crypto_connection_watchdog_task = watchdog_task
                    try:
                        authed = False
                        async for raw in ws:
                            for ev in _parse_massive_events(raw):
                                ev_type = ev.get("ev")
                                if ev_type == "status":
                                    status = ev.get("status")
                                    if status == "auth_success":
                                        authed = True
                                        async with self._massive_crypto_connect_lock:
                                            self._massive_crypto_authed = True
                                            active_now = set(self._massive_crypto_active_symbols)
                                            if active_now:
                                                await ws.send(json.dumps({
                                                    "action": "subscribe",
                                                    "params": _massive_crypto_ws_params_batch(active_now),
                                                }))
                                                self._massive_crypto_subscribed |= active_now
                                    elif status == "error":
                                        raise RuntimeError(
                                            f"massive_crypto_auth_or_subscribe_error: {ev.get('message')}"
                                        )
                                    continue
                                if not authed:
                                    continue
                                if ev_type == "XQ":
                                    pair = ev.get("pair")
                                    sym = _massive_crypto_symbol_from_pair(pair) if pair else None
                                    if sym is None or sym not in self._massive_crypto_active_symbols:
                                        continue
                                    try:
                                        bid = float(ev["bp"])
                                        ask = float(ev["ap"])
                                        ts  = int(ev["t"]) // 1000
                                    except (KeyError, TypeError, ValueError):
                                        continue
                                    if not _validate_quote_values(sym, bid, ask):
                                        continue
                                    self._massive_crypto_last_quote_at[sym] = time.monotonic()
                                    self._massive_crypto_symbol_stale_attempts[sym] = 0
                                    backoff = 2.0
                                    # CRYPTO-QUOTE-DEDUP-01 — freshness (the
                                    # two lines above: connection is alive,
                                    # backoff resets) and "this is new price
                                    # information" are separate concepts. An
                                    # exact repeat of the last (bid, ask) this
                                    # loop actually broadcast for THIS symbol
                                    # still refreshes _price_ts/Redis/
                                    # observability via _update_price_state()
                                    # (has_price()/get_validated_quote() stay
                                    # correctly fresh) but is never sent to
                                    # channel_layer — no group_send, no
                                    # price_tick, no candle_update, no
                                    # redundant visual movement. Any real
                                    # change in bid OR ask takes the full
                                    # _broadcast() path, unchanged.
                                    if self._massive_crypto_last_broadcast_quote.get(sym) == (bid, ask):
                                        await self._update_price_state(sym, bid, ask, source="massive")
                                        continue
                                    self._massive_crypto_last_broadcast_quote[sym] = (bid, ask)
                                    await self._broadcast(sym, channel_layer, bid, ask, ts, source="massive")
                                    continue
                                if ev_type == "XT":
                                    # MASSIVE-CRYPTO-TRADE-CANDLES-01 —
                                    # trade channel: chart/candle/volume
                                    # ONLY. Deliberately never touches
                                    # _massive_crypto_last_quote_at/
                                    # _massive_crypto_symbol_stale_attempts/
                                    # backoff — those are the XQ connection-
                                    # freshness signals; a trade says
                                    # nothing about quote staleness, and
                                    # must never mask a genuinely stale
                                    # quote feed. Never calls _broadcast()/
                                    # _update_price_state() — see
                                    # _broadcast_trade()'s own docstring.
                                    pair = ev.get("pair")
                                    sym = _massive_crypto_symbol_from_pair(pair) if pair else None
                                    if sym is None or sym not in self._massive_crypto_active_symbols:
                                        continue
                                    try:
                                        price = float(ev["p"])
                                        size  = float(ev.get("s", 0.0))
                                        ts    = int(ev["t"]) // 1000
                                    except (KeyError, TypeError, ValueError):
                                        continue
                                    # Reuses _validate_quote_values()'s Capa
                                    # A magnitude-plausibility band by
                                    # treating the single trade price as a
                                    # degenerate zero-spread quote (bid==
                                    # ask==price) — ask>=bid holds trivially
                                    # when equal, and mid==price exactly, so
                                    # this correctly validates the trade
                                    # price's magnitude without a second,
                                    # parallel validator function.
                                    if not _validate_quote_values(sym, price, price):
                                        continue
                                    await self._broadcast_trade(sym, channel_layer, price, size, ts)
                                    continue
                    finally:
                        watchdog_task.cancel()
                        try:
                            await watchdog_task
                        except asyncio.CancelledError:
                            pass
                        self._massive_crypto_connection_watchdog_task = None
                self._massive_crypto_subscribed.clear()
                self._massive_crypto_ws = None
                self._massive_crypto_authed = False
                if watchdog_state["reason"] == "connection_stale":
                    log.warning("[feed] Massive crypto shared connection reconnecting after connection-wide staleness")
                    backoff = 2.0
                    continue
                log.warning("[feed] Massive crypto shared connection ended — reconnect in %.0fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            except asyncio.CancelledError:
                self._massive_crypto_subscribed.clear()
                self._massive_crypto_ws = None
                self._massive_crypto_authed = False
                raise
            except Exception as exc:
                self._massive_crypto_subscribed.clear()
                self._massive_crypto_ws = None
                self._massive_crypto_authed = False
                if watchdog_state["reason"] == "connection_stale":
                    log.warning("[feed] Massive crypto shared connection reconnecting after connection-wide staleness")
                    backoff = 2.0
                    continue
                log.error(
                    "[feed] Massive crypto shared connection error: %r — reconnect in %.0fs",
                    exc, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

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

        # ── Crypto: Massive last-trade (GOLDEN-MARKETDATA-CRYPTO-01) ──
        # Checked FIRST for the 2 Massive-crypto symbols, fails CLOSED —
        # no fallthrough to Binance/CoinGecko/Kraken below for these 2.
        # Never fabricate a "recovered" price via a second provider (same
        # policy as the Forex Massive-symbols carve-out further down);
        # get_validated_quote() already refuses a stale/missing price for
        # any financial decision, so the only cost of waiting for a fresh
        # Massive WS tick instead is UX continuity during cold start, not
        # safety. This endpoint (/v2/last/trade) has no Forex equivalent
        # wired today (Design Lock §E) — confirmed live, response shape
        # {"results":{"p":price,...},"status":"OK"}.
        if MASSIVE_API_KEY and symbol in _MASSIVE_CRYPTO_ENABLED_SYMBOLS:
            mc_ticker = _massive_crypto_sym(symbol)
            if mc_ticker:
                try:
                    req = urllib.request.Request(
                        f"https://api.massive.com/v2/last/trade/{mc_ticker}",
                        headers={
                            "Authorization": f"Bearer {MASSIVE_API_KEY}",
                            "User-Agent": "trx-sim/1.0",
                        },
                    )

                    def _fetch_mc(r=req) -> bytes:
                        with urllib.request.urlopen(r, timeout=5) as resp:
                            return resp.read()

                    data = json.loads(await loop.run_in_executor(None, _fetch_mc))
                    if data.get("status") == "OK":
                        px = float((data.get("results") or {})["p"])
                        log.debug("[feed] Massive-crypto last-trade %s = %.4f", symbol, px)
                        return px
                    log.debug("[feed] Massive-crypto last-trade non-OK status for %s: %r",
                              symbol, data.get("status"))
                except Exception as exc:
                    log.debug("[feed] Massive-crypto last-trade unavailable for %s: %r", symbol, exc)
            return None

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
        # B2-FOREX-PROVIDER-CLEANUP-01 §6 — Massive is the sole runtime
        # provider for these 4 active Forex pairs; no Finnhub REST
        # resync for them either. Never fabricate a "recovered" price
        # via a second provider — wait for a fresh Massive WS tick
        # instead (get_validated_quote() already refuses a stale/missing
        # price for any financial decision, so the only cost here is UX
        # continuity during cold start, not safety). Any other "/"
        # symbol (disabled pairs, metal) is untouched and still falls
        # through to Finnhub REST below.
        if FINNHUB_API_KEY and "/" in symbol and symbol not in _MASSIVE_WS_ENABLED_SYMBOLS:
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
