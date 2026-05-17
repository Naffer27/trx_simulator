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
import os
import random
import time
import urllib.request
import urllib.error

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


# ─── symbol helpers ────────────────────────────────────────────────────────────

def _step_dec(symbol: str):
    if symbol in ("BTCUSD", "ETHUSD"): return (0.01, 2)
    if symbol.endswith("/JPY"):         return (0.001, 3)
    if "/" in symbol:                   return (0.00001, 5)
    return (0.00001, 5)

def _spread(symbol: str) -> float:
    if symbol == "BTCUSD":          return 0.30
    if symbol == "ETHUSD":          return 0.10
    if symbol.endswith("/JPY"):      return 0.004
    if "/" in symbol:                return 0.00002
    return 0.00002

def _drift(symbol: str) -> float:
    if symbol == "BTCUSD": return 12.0
    if symbol == "ETHUSD": return 2.0
    return 0.0008

def _fallback_price(symbol: str) -> float:
    """Last-resort hardcoded price — only used if REST resync also fails."""
    return {
        "EUR/USD": 1.13000, "GBP/USD": 1.33000, "USD/JPY": 145.000,
        "AUD/USD": 0.65000, "BTCUSD": 82000.0,  "ETHUSD": 3400.0,
    }.get(symbol, 1.0)

def _binance_sym(symbol: str) -> str | None:
    s = symbol.replace("/", "").upper()
    return {"BTCUSD": "BTCUSDT", "ETHUSD": "ETHUSDT"}.get(s)

def _kraken_sym(symbol: str) -> str | None:
    """Map internal symbol → Kraken WS pair name (returns None if unsupported)."""
    return {"BTCUSD": "XBT/USD", "ETHUSD": "ETH/USD"}.get(symbol)

def _finnhub_sym(symbol: str) -> str:
    s = symbol.upper()
    if "/" in s:
        a, b = s.split("/", 1)
        return f"FX:{a}{b}"
    return s


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

    # ── public API ──

    @staticmethod
    def group_for(symbol: str) -> str:
        return "feed_" + symbol.replace("/", "_")

    def last_price(self, symbol: str) -> float:
        return self._prices.get(symbol, _fallback_price(symbol))

    def last_bid(self, symbol: str) -> float:
        return self._bids.get(symbol, _fallback_price(symbol) - _spread(symbol) / 2)

    def last_ask(self, symbol: str) -> float:
        return self._asks.get(symbol, _fallback_price(symbol) + _spread(symbol) / 2)

    async def subscribe(self, symbol: str, channel_layer, channel_name: str) -> None:
        await channel_layer.group_add(self.group_for(symbol), channel_name)
        self._counts[symbol] = self._counts.get(symbol, 0) + 1
        self._ensure_running(symbol, channel_layer)

    async def unsubscribe(self, symbol: str, channel_layer, channel_name: str) -> None:
        await channel_layer.group_discard(self.group_for(symbol), channel_name)
        count = max(0, self._counts.get(symbol, 1) - 1)
        self._counts[symbol] = count
        if count <= 0:
            self._stop(symbol)

    # ── internal ──

    def _ensure_running(self, symbol: str, channel_layer) -> None:
        task = self._tasks.get(symbol)
        if task is None or task.done():
            self._tasks[symbol] = asyncio.create_task(
                self._feed_loop(symbol, channel_layer),
                name=f"feed_{symbol}",
            )
            log.info("[feed] started task for %s", symbol)

    def _stop(self, symbol: str) -> None:
        task = self._tasks.pop(symbol, None)
        if task and not task.done():
            task.cancel()
            log.info("[feed] stopped task for %s (no subscribers)", symbol)
        self._prices.pop(symbol, None)
        self._bids.pop(symbol, None)
        self._asks.pop(symbol, None)
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
        _KR_PAIRS   = {"BTCUSD": "XBTUSD",   "ETHUSD": "XETHZUSD"}
        _KR_INTERVAL = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "1d": 1440}
        kr_pair  = _KR_PAIRS.get(symbol)
        kr_intv  = _KR_INTERVAL.get(interval)
        if kr_pair and kr_intv:
            try:
                raw  = await loop.run_in_executor(
                    None, _fetch,
                    f"https://api.kraken.com/0/public/OHLC?pair={kr_pair}&interval={kr_intv}",
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
                log.debug("[feed] Kraken klines unavailable for %s: %r", symbol, exc)

        log.error("[feed] All kline history sources failed for %s %s", symbol, interval)
        return []

    async def _broadcast(self, symbol: str, cl, bid: float, ask: float, ts: int) -> None:
        _, dec = _step_dec(symbol)
        self._bids[symbol]   = bid
        self._asks[symbol]   = ask
        self._prices[symbol] = round((bid + ask) / 2, dec)
        await cl.group_send(
            self.group_for(symbol),
            {
                "type":   "price.tick",
                "symbol": symbol,
                "bid":    bid,
                "ask":    ask,
                "mid":    self._prices[symbol],
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
                await self._broadcast(symbol, channel_layer, bid, ask, int(time.time()))
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("[feed] sim error %s: %r", symbol, exc)
                await asyncio.sleep(1)

    # ── live feed loops ──

    async def _binance_loop(self, symbol: str, mapped: str, channel_layer) -> None:
        url = (
            f"wss://stream.binance.com:9443/stream"
            f"?streams={mapped}@bookTicker/{mapped}@kline_1m"
        )
        log.info("[feed] Binance loop for %s (%s)", symbol, mapped)
        consecutive_failures = 0
        MAX_FAILURES = 3
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
                                await self._broadcast(symbol, channel_layer, b, a, int(time.time()))
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
                    raise
                log.error(
                    "[feed] Binance error %s: %r — reconnect in 3s (%d/%d)",
                    symbol, exc, consecutive_failures, MAX_FAILURES,
                )
                await asyncio.sleep(3)

    async def _kraken_loop(self, symbol: str, kr_pair: str, channel_layer) -> None:
        """
        Kraken WS v1 — provides both ticker (bid/ask) and ohlc-1 (1m candles).
        Used as fallback when Binance WS is unavailable.
        Candles are broadcast via _broadcast_kline so candle_kline() picks them up.
        """
        url = "wss://ws.kraken.com"
        _, dec = _step_dec(symbol)
        spr    = _spread(symbol)
        log.info("[feed] Kraken loop for %s (%s)", symbol, kr_pair)
        consecutive_failures = 0
        MAX_FAILURES = 3

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
                                await self._broadcast(symbol, channel_layer, bid, ask, int(time.time()))

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
                    raise
                log.error("[feed] Kraken error %s: %r — reconnect in 3s (%d/%d)", symbol, exc, consecutive_failures, MAX_FAILURES)
                await asyncio.sleep(3)

    async def _finnhub_loop(self, symbol: str, channel_layer) -> None:
        finnhub_sym = _finnhub_sym(symbol)
        url = f"wss://ws.finnhub.io?token={FINNHUB_API_KEY}"
        _, dec = _step_dec(symbol)
        log.info("[feed] Finnhub loop for %s (%s)", symbol, finnhub_sym)
        consecutive_failures = 0
        MAX_FAILURES = 3
        while True:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20, ping_timeout=20,
                    close_timeout=10, max_queue=256,
                ) as ws:
                    consecutive_failures = 0
                    await ws.send(json.dumps({"type": "subscribe", "symbol": finnhub_sym}))
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("type") != "trade":
                            continue
                        for t in msg.get("data", []):
                            px = float(t.get("p") or 0.0)
                            if not px:
                                continue
                            spr = _spread(symbol)
                            bid = round(px - spr / 2, dec)
                            ask = round(px + spr / 2, dec)
                            ts  = int((t.get("t") or time.time() * 1000) / 1000)
                            await self._broadcast(symbol, channel_layer, bid, ask, ts)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_failures += 1
                if consecutive_failures >= MAX_FAILURES:
                    log.warning(
                        "[feed] Finnhub giving up for %s after %d failures",
                        symbol, consecutive_failures,
                    )
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
            self._prices[symbol] = mid
            self._bids[symbol]   = round(mid - spr / 2, dec)
            self._asks[symbol]   = round(mid + spr / 2, dec)
            log.info("[feed] resynced %s → %.4f", symbol, mid)
        else:
            # Keep whatever we have; only fall to hardcoded if nothing stored
            if symbol not in self._prices:
                self._prices[symbol] = _fallback_price(symbol)
                log.warning(
                    "[feed] REST resync failed for %s — using fallback %.2f",
                    symbol, self._prices[symbol],
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
