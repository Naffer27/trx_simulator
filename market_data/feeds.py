"""
Shared per-process market data feed manager.

One asyncio task per symbol serves ALL connected consumers via Django
Channels group "feed_{symbol}". Each TradingConsumer subscribes on
connect and unsubscribes on disconnect — no per-user upstream connections.

Multi-process note: with InMemoryChannelLayer each process runs its own
FeedManager (one Binance connection per process per symbol). With Redis
channel layer, ticks route across workers automatically; to reduce to a
single upstream connection across all workers, run a dedicated management
command instead (future work).
"""

import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime

try:
    import websockets
except ImportError:
    websockets = None

log = logging.getLogger("simulator.ws")

FINNHUB_API_KEY = (os.getenv("FINNHUB_API_KEY", "") or "").strip()
DEFAULT_TICK_INTERVAL = float(os.getenv("PRICE_TICK_INTERVAL", "1.0"))


# ------------- symbol helpers -------------

def _step_dec(symbol: str):
    if symbol in ("BTCUSD", "ETHUSD"): return (0.01, 2)
    if symbol.endswith("/JPY"):         return (0.001, 3)
    if "/" in symbol:                   return (0.00001, 5)
    return (0.00001, 5)

def _spread(symbol: str) -> float:
    if symbol == "BTCUSD":          return 0.3
    if symbol == "ETHUSD":          return 0.1
    if symbol.endswith("/JPY"):      return 0.004
    if "/" in symbol:                return 0.00002
    return 0.00002

def _drift(symbol: str) -> float:
    if symbol == "BTCUSD": return 12.0
    if symbol == "ETHUSD": return 2.0
    return 0.0008

def _base_price(symbol: str) -> float:
    return {
        "EUR/USD": 1.17000, "GBP/USD": 1.30000, "USD/JPY": 155.000,
        "AUD/USD": 0.68000, "BTCUSD": 68000.0,  "ETHUSD": 3400.0,
    }.get(symbol, 1.17000)

def _binance_mapped(symbol: str):
    s = symbol.replace("/", "").upper()
    return {"BTCUSD": "BTCUSDT", "ETHUSD": "ETHUSDT"}.get(s)

def _finnhub_sym(symbol: str) -> str:
    s = symbol.upper()
    if "/" in s:
        a, b = s.split("/", 1)
        return f"FX:{a}{b}"
    return s


# ------------- singleton accessor -------------

_MANAGER = None

def get_feed_manager() -> "FeedManager":
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = FeedManager()
    return _MANAGER


# ------------- FeedManager -------------

class FeedManager:
    """
    Manages one upstream feed task per symbol.
    Broadcasts price.tick events to Channels group "feed_{safe_sym}".
    """

    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}
        self._counts: dict[str, int] = {}
        self._prices: dict[str, float] = {}

    # --- public API ---

    @staticmethod
    def group_for(symbol: str) -> str:
        return "feed_" + symbol.replace("/", "_")

    def last_price(self, symbol: str) -> float:
        return self._prices.get(symbol, _base_price(symbol))

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

    # --- internal ---

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

    async def _broadcast(self, symbol: str, cl, bid: float, ask: float, ts: int) -> None:
        _, dec = _step_dec(symbol)
        self._prices[symbol] = round((bid + ask) / 2, dec)
        await cl.group_send(
            self.group_for(symbol),
            {
                "type": "price.tick",
                "symbol": symbol,
                "bid": bid,
                "ask": ask,
                "mid": self._prices[symbol],
                "time": ts,
            },
        )

    async def _feed_loop(self, symbol: str, channel_layer) -> None:
        """Decide provider, run loop with automatic fallback to sim."""
        mapped = _binance_mapped(symbol)

        if mapped and websockets:
            try:
                await self._binance_loop(symbol, mapped, channel_layer)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("[feed] Binance failed for %s (%r) — sim fallback", symbol, exc)

        if FINNHUB_API_KEY and websockets and "/" in symbol:
            try:
                await self._finnhub_loop(symbol, channel_layer)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("[feed] Finnhub failed for %s (%r) — sim fallback", symbol, exc)

        await self._sim_loop(symbol, channel_layer)

    async def _sim_loop(self, symbol: str, channel_layer) -> None:
        log.info("[feed] sim loop started for %s", symbol)
        interval = DEFAULT_TICK_INTERVAL
        _, dec = _step_dec(symbol)
        while True:
            try:
                mid = self._prices.get(symbol, _base_price(symbol))
                mid += (random.random() - 0.5) * _drift(symbol)
                spr = _spread(symbol)
                bid = round(mid - spr / 2, dec)
                ask = round(mid + spr / 2, dec)
                ts = int(datetime.utcnow().timestamp())
                await self._broadcast(symbol, channel_layer, bid, ask, ts)
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("[feed] sim loop error %s: %r", symbol, exc)
                await asyncio.sleep(1)

    async def _binance_loop(self, symbol: str, mapped: str, channel_layer) -> None:
        url = (
            f"wss://stream.binance.com:9443/stream"
            f"?streams={mapped}@bookTicker/{mapped}@kline_1s"
        )
        log.info("[feed] Binance loop for %s (%s)", symbol, mapped)
        while True:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20, ping_timeout=20,
                    close_timeout=10, max_queue=256,
                ) as ws:
                    async for raw in ws:
                        obj = json.loads(raw)
                        stream = obj.get("stream") or ""
                        data = obj.get("data") or {}
                        if stream.endswith("@bookTicker"):
                            b = float(data.get("b") or 0.0)
                            a = float(data.get("a") or 0.0)
                            if a > b > 0:
                                await self._broadcast(symbol, channel_layer, b, a, int(time.time()))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("[feed] Binance error %s: %r — reconnect in 5s", symbol, exc)
                await asyncio.sleep(5)

    async def _finnhub_loop(self, symbol: str, channel_layer) -> None:
        finnhub_sym = _finnhub_sym(symbol)
        url = f"wss://ws.finnhub.io?token={FINNHUB_API_KEY}"
        _, dec = _step_dec(symbol)
        log.info("[feed] Finnhub loop for %s (%s)", symbol, finnhub_sym)
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
                        if msg.get("type") != "trade":
                            continue
                        for t in msg.get("data", []):
                            px = float(t.get("p") or 0.0)
                            if not px:
                                continue
                            spr = _spread(symbol)
                            bid = round(px - spr / 2, dec)
                            ask = round(px + spr / 2, dec)
                            ts = int((t.get("t") or time.time() * 1000) / 1000)
                            await self._broadcast(symbol, channel_layer, bid, ask, ts)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("[feed] Finnhub error %s: %r — reconnect in 5s", symbol, exc)
                await asyncio.sleep(5)
