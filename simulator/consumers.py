# simulator/consumers.py
import os, json, asyncio, random, time, logging
from datetime import datetime
from urllib.parse import parse_qs

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.db import transaction

from market_data.feeds import get_feed_manager
from market_data.symbol_specs import get_spec, allowed_symbols, kline_symbols
from .models import TradingAccount, Position, Trade, LedgerEntry, BrokerLedger
from .spread_engine import broker_price, calculate_spread_revenue, _get_config as _get_spread_config
from .observability import security_log

log = logging.getLogger("simulator.ws")

FINNHUB_API_KEY = (os.getenv("FINNHUB_API_KEY", "") or "").strip()
DEFAULT_TICK_INTERVAL = float(os.getenv("PRICE_TICK_INTERVAL", "1.0"))

# Derived from symbol registry — no manual maintenance needed.
_KLINE_SYMBOLS   = kline_symbols()   # symbols with exchange kline stream (Binance/Kraken)
_ALLOWED_SYMBOLS = allowed_symbols() # whitelist: rejects unknown symbols at the WS boundary

# ---------------- TF helpers ----------------
def tf_seconds(tf: str) -> int:
    s = str(tf).strip().lower()
    alias = {
        "1": "1s","1sec":"1s","1second":"1s","1s":"1s",
        "60":"1m","60s":"1m","m1":"1m","1m":"1m","1min":"1m",
        "300":"5m","m5":"5m","5m":"5m",
        "900":"15m","m15":"15m","15m":"15m",
        "3600":"1h","h1":"1h","1h":"1h",
        "86400":"1d","d1":"1d","1d":"1d",
    }
    s = alias.get(s, s)
    return {"1s":1,"1m":60,"5m":300,"15m":900,"1h":3600,"1d":86400}.get(s, 1)

def normalize_tf(tf: str) -> str:
    rev = {1:"1s",60:"1m",300:"5m",900:"15m",3600:"1h",86400:"1d"}
    return rev.get(tf_seconds(tf), "1s")

# ---------------- Símbolos / formatos ----------------
# Thin wrappers — all instrument parameters come from the symbol registry.

def step_decimals_for(symbol: str) -> tuple[float, int]:
    sp = get_spec(symbol)
    return (sp.tick_size, sp.price_decimals)

def spread_for(symbol: str) -> float:
    return get_spec(symbol).spread

def drift_for(symbol: str) -> float:
    return get_spec(symbol).sim_drift

def base_price_for(symbol: str) -> float:
    return get_spec(symbol).base_price


# ── Phase 6B.1 — Pre-Trade Margin Guard ──────────────────────────────────────
# Default caps applied to all accounts. Product snapshots can supply
# tighter per-account values (margin_call_level_snapshot, max_lot_size_snapshot,
# allowed_symbols_snapshot) that override these global defaults.

_DEFAULT_MAX_MARGIN_PER_TRADE_PCT = 10.0   # single-trade margin / equity ≤ 10 %
_DEFAULT_MAX_TOTAL_MARGIN_PCT     = 50.0   # total margin after open / equity ≤ 50 %


def _compute_pretrade_margin_guard(
    symbol: str,
    qty: float,
    entry_px: float,
    equity: float,
    margin_used_now: float,
    account_snap: dict,
    spec_max_leverage: int,
    spec_contract_size: float,
) -> tuple[bool, str, str]:
    """
    Pure pre-trade guard — no I/O, no DB, no side effects.

    Returns (ok, code, user_message).
      ok=True  → order may proceed
      ok=False → order rejected; code and message sent to the frontend

    Checks (in order):
      1. allowed_symbols_snapshot  — symbol whitelist
      2. max_lot_size_snapshot     — product-level hard lot cap
      3. per-trade margin %        — required_margin / equity ≤ 10 %
      4. total margin after open % — (used + required) / equity ≤ 50 %
      5. margin_level projection   — equity / (used + required) ≥ margin_call_level_snapshot
    """
    # 1 — Symbol whitelist (None = all symbols allowed)
    allowed = account_snap.get("allowed_symbols")
    if allowed is not None and symbol not in allowed:
        return (
            False,
            "symbol_not_allowed",
            "Orden rechazada: símbolo no permitido para esta cuenta.",
        )

    # 2 — Product max lot size snapshot
    max_lot = account_snap.get("max_lot_size")
    if max_lot is not None and qty > float(max_lot):
        return (
            False,
            "lot_size_exceeds_product_limit",
            (
                f"Orden rechazada: el tamaño es demasiado alto para esta cuenta. "
                f"Máximo permitido: {float(max_lot):.3f} lotes. Prueba con un lote menor."
            ),
        )

    # 3 — Compute required margin
    account_lev = max(1, int(account_snap.get("leverage", 50)))
    effective_lev = max(1, min(account_lev, spec_max_leverage))
    required_margin = abs(entry_px * qty * spec_contract_size) / effective_lev
    equity = max(float(equity), 0.01)

    per_trade_pct = required_margin / equity * 100.0
    if per_trade_pct > _DEFAULT_MAX_MARGIN_PER_TRADE_PCT:
        return (
            False,
            "margin_per_trade_exceeded",
            (
                f"Orden rechazada: margen insuficiente. Esta operación requeriría "
                f"{per_trade_pct:.1f}% de tu equity como margen "
                f"(límite: {_DEFAULT_MAX_MARGIN_PER_TRADE_PCT:.0f}%). "
                "Prueba con un lote menor."
            ),
        )

    # 4 — Total margin cap after this trade
    total_margin_after = float(margin_used_now) + required_margin
    total_margin_pct = total_margin_after / equity * 100.0
    if total_margin_pct > _DEFAULT_MAX_TOTAL_MARGIN_PCT:
        return (
            False,
            "total_margin_exceeded",
            (
                f"Orden rechazada: esta operación excedería el uso máximo de margen "
                f"permitido ({_DEFAULT_MAX_TOTAL_MARGIN_PCT:.0f}%). "
                f"Margen total proyectado: {total_margin_pct:.1f}%. "
                "Cierra posiciones o usa un lote menor."
            ),
        )

    # 5 — Margin level projection vs margin_call_level_snapshot
    margin_call_level = float(account_snap.get("margin_call_level") or 100.0)
    if total_margin_after > 0:
        margin_level_after = equity / total_margin_after * 100.0
        if margin_level_after < margin_call_level:
            return (
                False,
                "margin_call_level_breach",
                (
                    f"Orden rechazada: margen insuficiente. "
                    f"El nivel de margen proyectado ({margin_level_after:.1f}%) quedaría "
                    f"por debajo del límite de tu cuenta ({margin_call_level:.0f}%). "
                    "Prueba con un lote menor."
                ),
            )

    return True, "ok", ""


# ======================================================
#                       CONSUMER
# ======================================================
class TradingConsumer(AsyncWebsocketConsumer):

    # ---------------- Conexión ----------------
    async def connect(self):
        self._db_account_id = None
        self._last_db_sync = 0.0

        user = self.scope.get("user")
        is_auth = bool(user and getattr(user, "is_authenticated", False))

        # Querystring
        try:
            qs = parse_qs(self.scope.get("query_string", b"").decode())
            q_account_raw = qs.get("account",[None])[0] or qs.get("account_id",[None])[0]
            q_account = int(q_account_raw) if q_account_raw else None
            q_tf_raw = qs.get("tf",[None])[0] or qs.get("timeframe",[None])[0]
        except Exception:
            q_account = None
            q_tf_raw = None

        uname = getattr(user, "username", None)
        log.info("[connect] user=%s is_auth=%s q_account=%s", uname, is_auth, q_account)

        if not is_auth:
            client = self.scope.get("client")
            ip = client[0] if isinstance(client, (list, tuple)) and client else str(client)
            log.warning("[connect] rejected unauthenticated WS from %s", ip)
            security_log("ws.rejected_unauthenticated", ip=ip)
            await self.close(code=4001)
            return

        # Priority 0: account_id in WS URL path  ws/trading/<account_id>/
        if is_auth and not self._db_account_id:
            url_account_id = self.scope.get("url_route", {}).get("kwargs", {}).get("account_id")
            if url_account_id:
                acc = await self._db_get_account_for_user(int(url_account_id), user.id)
                if acc:
                    self._db_account_id = acc["id"]
                    log.info("[connect] db_account_id=%s (from URL path)", self._db_account_id)

        # Priority 1: querystring ?account=<id>
        if is_auth and not self._db_account_id and q_account:
            acc = await self._db_get_account_for_user(q_account, user.id)
            if acc:
                self._db_account_id = acc["id"]
                log.info("[connect] db_account_id=%s (from URL param)", self._db_account_id)

        # Fallback 3: account_id stored in Django session by login_view
        if is_auth and not self._db_account_id:
            session = self.scope.get("session", {})
            sess_acc_id = session.get("account_id")
            log.info("[connect] session account_id=%s", sess_acc_id)
            if sess_acc_id:
                acc = await self._db_get_account_for_user(int(sess_acc_id), user.id)
                if acc:
                    self._db_account_id = acc["id"]
                    log.info("[connect] db_account_id=%s (from session)", self._db_account_id)

        # Fallback 4: most-recent active account for this user
        if is_auth and not self._db_account_id:
            acc = await self._db_get_latest_account_for_user(getattr(user, "id", None))
            if acc:
                self._db_account_id = acc["id"]
                log.info("[connect] db_account_id=%s (from DB fallback)", self._db_account_id)

        if not self._db_account_id:
            log.warning("[connect] NO db_account_id resolved — all DB writes will be skipped")

        await self.accept()
        if self._db_account_id and self.channel_layer:
            await self.channel_layer.group_add(
                f"account_{self._db_account_id}", self.channel_name
            )
        await self._ws_counter(1)

        # --- Estado inicial (memoria) ---
        self.symbol = "EUR/USD"
        self.timeframe = normalize_tf(q_tf_raw or "1m")
        self._price_state = {}   # mid price por símbolo
        self._bid_state   = {}   # bid (sell/close-buy) por símbolo
        self._ask_state   = {}   # ask (buy/close-sell) por símbolo
        self._order_seq = 1
        self._positions = []
        self._agg = {}
        self._last_bar_time = {}

        self.account = {
            "balance":       0.0,
            "equity":        0.0,
            "peak_balance":  0.0,
            "pnl_unreal":    0.0,
            "margin_used":   0.0,
            "leverage":      50,
            "netting_mode":  False,
            "status":        "Activo",
            "account_type":  "CHALLENGE",
            "tier":          "",
            "profit_target": 0.0,
            "initial_balance": 0.0,
            # Phase 6B — product rule defaults (overwritten by hydration if snapshot set)
            "product_name":       "",
            "commission_per_lot": 0.0,
            "spread_pips":        0.0,
            "allowed_symbols":    None,
            "max_lot_size":       None,
            "margin_call_level":  100.0,
            "stopout_level":      50.0,
        }
        self._daily_realized_pnl = 0.0
        self._daily_pnl_date = None

        await self._maybe_hydrate_from_db()

        # Shared feed subscription
        self._feed = get_feed_manager()
        self._seed_price_state(self.symbol)
        await self._feed.subscribe(self.symbol, self.channel_layer, self.channel_name)

        # Heartbeat — closes stale connections after 90 s of client silence
        self._last_msg_ts = time.time()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        await self.send_positions_snapshot()
        await self._recalc_account_and_push()
        await self.send_json({"type":"ack","action":"connected",
                              "timeframe":self.timeframe,"tf_sec":tf_seconds(self.timeframe)})

    async def disconnect(self, close_code):
        await self._ws_counter(-1)
        # Cancel heartbeat
        hb = getattr(self, "_heartbeat_task", None)
        if hb and not hb.done():
            hb.cancel()
        # Leave daemon notification group
        if getattr(self, "_db_account_id", None) and self.channel_layer:
            await self.channel_layer.group_discard(
                f"account_{self._db_account_id}", self.channel_name
            )
        # Unsubscribe from shared feed
        feed = getattr(self, "_feed", None)
        if feed:
            await feed.unsubscribe(self.symbol, self.channel_layer, self.channel_name)

    # ---------------- Mensajes entrantes ----------------
    async def receive(self, text_data: str):
        self._last_msg_ts = time.time()
        try:
            data = json.loads(text_data)
        except Exception:
            await self.send_json({"type":"error","message":"invalid_json"})
            return

        act = data.get("action")

        if act == "ping":
            await self.send_json({"type": "pong", "ts": int(time.time())})
            return

        if act == "change_symbol":
            new_sym = data.get("symbol", self.symbol)
            if new_sym not in _ALLOWED_SYMBOLS:
                await self.send_json({"type": "error", "code": "invalid_symbol", "message": "simbolo_no_permitido"})
                return
            old_sym = self.symbol
            if new_sym != old_sym:
                await self._feed.unsubscribe(old_sym, self.channel_layer, self.channel_name)
                self.symbol = new_sym
                self._reset_agg(new_sym)
                self._seed_price_state(new_sym)
                await self._feed.subscribe(new_sym, self.channel_layer, self.channel_name)
            self._last_bar_time.pop(new_sym, None)
            hist = await self.generate_history(new_sym, self.timeframe, bars=240)
            await self.send_json({"type": "history", "symbol": new_sym, "data": hist})
            await self._send_bridge_candle(new_sym, self.timeframe)
            await self.send_json({"type": "ack", "action": "symbol_changed", "symbol": new_sym})
            await self.send_json({"type": "positions", "items": self._positions_snapshot()})

        elif act == "change_timeframe":
            tf = normalize_tf(data.get("timeframe", self.timeframe))
            self.timeframe = tf
            self._reset_agg(self.symbol)
            self._last_bar_time.pop(self.symbol, None)
            hist = await self.generate_history(self.symbol, tf, bars=240)
            await self.send_json({"type": "history", "symbol": self.symbol, "data": hist})
            await self._send_bridge_candle(self.symbol, tf)
            await self.send_json({"type":"ack","action":"change_timeframe","timeframe":tf,"tf_sec":tf_seconds(tf)})

        elif act == "load_history":
            sym = data.get("symbol", self.symbol)
            tf  = normalize_tf(data.get("timeframe", self.timeframe))
            hist = await self.generate_history(sym, tf, bars=240)
            await self.send_json({"type":"history","symbol":sym,"data":hist})
            await self._send_bridge_candle(sym, tf)

        elif act == "account:get":
            await self._recalc_account_and_push()

        elif act == "order:mode":
            nm = data.get("netting_mode", None)
            if isinstance(nm, bool):
                self.account["netting_mode"] = nm
                await self.send_json({"type":"info","message":f"netting_mode={nm}"})

        elif act == "order:risk_preview":
            await self._handle_risk_preview(data)

        elif act == "order:new":
            await self._order_new(data)

        elif act == "order:update":
            await self._order_update(data)

        elif act == "order:close":
            await self._order_close(data)

        else:
            await self.send_json({"type":"ack","ok":True,"action":act})

    # ---------------- Streams ----------------
    # ---------------- Shared feed handler ----------------

    async def execution_close(self, event: dict):
        """Daemon-initiated close pushed via account_{id} channel group.

        Updates in-memory state atomically then pushes order_close + positions
        so the live UI reflects the close without requiring a reconnect.
        """
        pos_id      = event.get("position_id")
        new_balance = event.get("new_balance")
        realized    = float(event.get("realized_pnl", 0.0))
        new_status  = event.get("new_status")

        # Remove from in-memory positions list
        before = len(self._positions)
        self._positions = [p for p in self._positions if p["id"] != pos_id]
        if len(self._positions) == before:
            log.warning("[execution_close] pos %s not found in memory (concurrent close?)", pos_id)

        # Apply authoritative DB result
        if new_balance is not None:
            self.account["balance"] = float(new_balance)
        if new_status:
            self.account["status"] = new_status

        self._track_daily_pnl(realized)
        await self._recalc_account_and_push()

        await self.send_json({
            "type":         "order_close",
            "id":           pos_id,
            "symbol":       event.get("symbol"),
            "side":         event.get("side"),
            "qty":          event.get("qty"),
            "avg":          event.get("avg"),
            "close_px":     event.get("close_px"),
            "reason":       event.get("reason"),
            "realized_pnl": realized,
            "ts":           event.get("ts", int(time.time())),
        })
        await self.send_json({"type": "positions", "items": self._positions_snapshot()})

        # Stopout / margin-call UI notifications (additive — only for daemon-initiated paths)
        if new_status == "Suspendido":
            await self.send_json({
                "type":   "account:suspended",
                "status": "Suspendido",
                "reason": event.get("reason"),
            })
        elif event.get("reason") == "daemon_margin_call" and not self._positions:
            await self.send_json({
                "type":    "account:margin_call",
                "reason":  "margin_level_below_50pct",
                "balance": float(new_balance) if new_balance is not None else 0.0,
            })

    async def price_tick(self, event: dict):
        """Receives broadcast ticks from FeedManager via channel layer group."""
        symbol = event.get("symbol")
        if symbol != self.symbol:
            return
        raw_bid = event["bid"]
        raw_ask = event["ask"]
        mid     = event["mid"]
        ts      = event["time"]

        bid, ask = broker_price(symbol, raw_bid, raw_ask)
        self.set_state(symbol, bid, ask, mid)
        await self.send_json({"type": "tick", "symbol": symbol, "bid": bid, "ask": ask, "time": ts})
        await self._on_tick(symbol, mid, volume=0.0, ts=ts)
        await self._check_tp_sl(symbol, bid, ask)
        await self._recalc_account_and_push()

    async def candle_kline(self, event: dict):
        """Receives canonical OHLCV from exchange kline stream (Binance @kline_1m).
        Bypasses server-side aggregation — the exchange owns candle lifecycle."""
        symbol = event.get("symbol")
        if symbol != self.symbol:
            return
        bar = event["data"]
        t   = int(bar["time"])
        last = self._last_bar_time.get(symbol)
        if last is None or t > last:
            self._last_bar_time[symbol] = t
            msg_type = "candle_new"
        else:
            msg_type = "candle_update"
        await self.send_json({
            "type": msg_type, "symbol": symbol,
            "data": {
                "time":  t,
                "open":  float(bar["open"]),
                "high":  float(bar["high"]),
                "low":   float(bar["low"]),
                "close": float(bar["close"]),
            },
        })
        await self.send_json({
            "type":   "volume_update",
            "symbol": symbol,
            "time":   t,
            "value":  float(bar.get("volume", 0.0)),
            "color":  "#26a69a" if float(bar["close"]) >= float(bar["open"]) else "#f44336",
        })

    # ---------------- Heartbeat ----------------

    async def _heartbeat_loop(self):
        """Send server ping every 30 s; close stale connections after 90 s silence."""
        PING_INTERVAL = 30
        STALE_TIMEOUT = 90
        while True:
            await asyncio.sleep(PING_INTERVAL)
            now = time.time()
            if now - self._last_msg_ts > STALE_TIMEOUT:
                log.warning("[heartbeat] stale connection for account=%s — closing", self._db_account_id)
                await self.close()
                return
            try:
                await self.send_json({"type": "heartbeat", "ts": int(now)})
            except Exception:
                return

    # ---------------- Agregador de velas ----------------
    def _reset_agg(self, symbol: str):
        self._agg[symbol] = {"t0":None,"o":None,"h":None,"l":None,"c":None,"v":0.0,"tf_sec":tf_seconds(self.timeframe)}

    async def _on_tick(self, symbol: str, price: float, volume: float = 0.0, ts: int | None = None):
        # Exchange-kline symbols send canonical OHLCV via candle_kline().
        # Server-side aggregation from price ticks would produce a second, divergent series.
        if symbol in _KLINE_SYMBOLS:
            return
        if ts is None: ts = int(time.time())
        acc = self._agg.get(symbol)
        if acc is None or acc["tf_sec"] != tf_seconds(self.timeframe):
            self._reset_agg(symbol)
            acc = self._agg[symbol]

        tf_sec = acc["tf_sec"]
        bucket = (ts // tf_sec) * tf_sec

        if acc["t0"] is None:
            acc["t0"]=bucket; acc["o"]=acc["h"]=acc["l"]=acc["c"]=price; acc["v"]=float(volume or 0.0)
            await self._emit_bar(symbol, acc); return

        if bucket == acc["t0"]:
            acc["c"]=price; acc["h"]=max(acc["h"],price); acc["l"]=min(acc["l"],price)
            acc["v"]=float(acc["v"])+float(volume or 0.0)
            await self._emit_bar(symbol, acc); return

        # bucket nuevo
        acc["t0"]=bucket; acc["o"]=acc["h"]=acc["l"]=acc["c"]=price; acc["v"]=float(volume or 0.0)
        await self._emit_bar(symbol, acc)

    async def _emit_bar(self, symbol: str, acc: dict):
        bar = {"time":int(acc["t0"]), "open":float(acc["o"]), "high":float(acc["h"]),
               "low":float(acc["l"]), "close":float(acc["c"])}
        last_time = self._last_bar_time.get(symbol)

        if last_time is None or int(acc["t0"]) > last_time:
            await self.send_json({"type":"candle_new","symbol":symbol,"data":bar})
            self._last_bar_time[symbol] = int(acc["t0"])
        else:
            await self.send_json({"type":"candle_update","symbol":symbol,"data":bar})

        await self.send_json({
            "type":"volume_update","symbol":symbol,"time":int(acc["t0"]),
            "value":float(acc.get("v",0.0)),
            "color":"#26a69a" if acc["c"]>=acc["o"] else "#f44336",
        })

    # ---------------- Historia sintética ----------------

    async def _send_bridge_candle(self, symbol: str, timeframe: str) -> None:
        """Send a flat candle at the CURRENT live bucket so the price line
        anchors to the real feed price immediately after history loads,
        eliminating the visual gap between synthetic history and live ticks."""
        px = self._price_state.get(symbol, base_price_for(symbol))
        _, dec = step_decimals_for(symbol)
        tf_sec = tf_seconds(timeframe)
        now = int(time.time())
        bucket = (now // tf_sec) * tf_sec
        px = round(px, dec)
        await self.send_json({
            "type": "candle_update",
            "symbol": symbol,
            "data": {"time": bucket, "open": px, "high": px, "low": px, "close": px},
        })
    async def generate_history(self, symbol, timeframe, bars=200):
        # For exchange-kline symbols, fetch real historical data from Binance REST.
        if symbol in _KLINE_SYMBOLS:
            hist = await self._feed.fetch_kline_history(symbol, interval=timeframe, limit=bars)
            if hist:
                # Snap the in-memory price state to the last closed bar so the
                # bridge candle and bid/ask calculations start at a real price.
                last_close = hist[-1]["close"]
                spr = spread_for(symbol)
                _, dec = step_decimals_for(symbol)
                self._price_state[symbol] = last_close
                self._bid_state[symbol], self._ask_state[symbol] = broker_price(
                    symbol,
                    round(last_close - spr / 2, dec),
                    round(last_close + spr / 2, dec),
                )
                return hist
            log.warning("[consumer] Binance REST history failed for %s — falling back to synthetic", symbol)

        # Synthetic history for non-Binance symbols (and Binance emergency fallback).
        base = self._price_state.get(symbol, base_price_for(symbol))
        step, dec = step_decimals_for(symbol)
        d = drift_for(symbol)
        now = int(time.time())
        tf_sec = tf_seconds(timeframe)
        current_bucket = (now // tf_sec) * tf_sec
        series = []
        price = base
        rnd = random.Random(symbol + timeframe)
        for i in range(1, bars + 1):
            ts = current_bucket - i * tf_sec
            c = price
            o = c + (rnd.random() - 0.5) * d
            h = max(o, c) + abs(rnd.random() - 0.5) * d * 0.6
            l = min(o, c) - abs(rnd.random() - 0.5) * d * 0.6
            price = o
            series.append({"time": ts, "open": round(o, dec), "high": round(h, dec),
                           "low": round(l, dec), "close": round(c, dec)})
        series.reverse()
        return series

    # ---------------- Estado de precio ----------------

    def _seed_price_state(self, symbol: str) -> None:
        """Seed bid/ask/mid from FeedManager on connect / symbol change."""
        raw_bid = self._feed.last_bid(symbol)
        raw_ask = self._feed.last_ask(symbol)
        self._bid_state[symbol], self._ask_state[symbol] = broker_price(symbol, raw_bid, raw_ask)
        self._price_state[symbol] = self._feed.last_price(symbol)

    def set_state(self, symbol, bid: float, ask: float, mid: float):
        self._bid_state[symbol]   = float(bid)
        self._ask_state[symbol]   = float(ask)
        self._price_state[symbol] = float(mid)

    def ensure_state(self, symbol) -> float:
        """Mid price — for candle aggregation and chart line only."""
        return self._price_state.get(symbol, base_price_for(symbol))

    def get_bid(self, symbol) -> float:
        return self._bid_state.get(symbol, base_price_for(symbol))

    def get_ask(self, symbol) -> float:
        return self._ask_state.get(symbol, base_price_for(symbol))

    def exec_price(self, symbol: str, side: str) -> float:
        """Fill price when OPENING: buy fills at ask, sell fills at bid."""
        return self.get_ask(symbol) if side == "buy" else self.get_bid(symbol)

    def close_price(self, symbol: str, side: str) -> float:
        """Fill price when CLOSING: buy closes at bid, sell closes at ask."""
        return self.get_bid(symbol) if side == "buy" else self.get_ask(symbol)

    # ---------------- Órdenes / Cuenta ----------------
    async def _order_new(self, data: dict):
        sym  = data.get("symbol", self.symbol)
        side = str(data.get("side","")).lower()   # 'buy' | 'sell'  (in-memory stays lowercase)
        qty  = float(data.get("qty",0) or 0)
        sl   = data.get("sl")
        tp   = data.get("tp")

        if sym not in _ALLOWED_SYMBOLS:
            await self.send_json({"type": "error", "code": "invalid_symbol", "message": "simbolo_no_permitido"})
            return

        # Rate limit: max 10 new orders per 10 seconds per account (Redis sliding window)
        if self._db_account_id:
            import django.conf as _dc
            _redis_url = getattr(_dc.settings, "REDIS_URL", "") or "redis://127.0.0.1:6379/0"
            from .observability import order_rate_check as _rate_check
            loop = asyncio.get_event_loop()
            allowed = await loop.run_in_executor(
                None, _rate_check, _redis_url, self._db_account_id
            )
            if not allowed:
                await self.send_json({"type": "error", "code": "rate_limited", "message": "demasiadas_ordenes"})
                return

        if side not in ("buy","sell") or qty <= 0:
            await self.send_json({"type":"error","code":"invalid_order","message":"orden_invalida"})
            return

        # Fast in-memory check (margin, min qty)
        ok, reason = self._pretrade_check(sym, side, qty)
        if not ok:
            await self.send_json({"type":"error","code":reason,"message":reason})
            await self._recalc_account_and_push()
            await self.send_json({"type":"positions","items":self._positions_snapshot()})
            return

        # Phase 6B.1 — per-product margin guard (snapshot-based, pure, no DB)
        eq_now = self.account["balance"] + self._unrealized_pnl_total()
        mg_now = self._margin_used_total()
        _spec  = get_spec(sym)
        _guard_ok, _guard_code, _guard_msg = _compute_pretrade_margin_guard(
            sym, qty, self.exec_price(sym, side), eq_now, mg_now,
            self.account, _spec.max_leverage, _spec.contract_size,
        )
        if not _guard_ok:
            await self.send_json({"type": "error", "code": _guard_code, "message": _guard_msg})
            return

        # ── Position risk assessment ──────────────────────────────────
        # eq_now / mg_now already computed above — reuse them.
        lev    = max(1, int(self.account.get("leverage", 50)))
        risk_assessment = await self._db_evaluate_risk(sym, qty, eq_now, mg_now, lev)
        risk_level = risk_assessment.get("risk_level", "LOW")

        if risk_level == "EXTREME":
            # Reject order without suspending account
            await self.send_json({
                "type": "order_rejected",
                "code": "extreme_risk",
                **risk_assessment,
            })
            return

        if risk_level == "HIGH" and not data.get("risk_confirmed"):
            # Require explicit client confirmation before executing
            await self.send_json({
                "type": "risk_warning",
                "requires_confirm": True,
                "pending_side": side,
                "pending_qty": qty,
                "pending_symbol": sym,
                **risk_assessment,
            })
            return
        # ─────────────────────────────────────────────────────────────

        # Risk engine gate (DB: lot size, positions count, daily dd, max dd, account status)
        risk_errors = await self._db_validate_order_risk(qty, len(self._positions), sym)
        _blocking = [e for e in risk_errors if e.get("blocking", True)]
        _warnings  = [e for e in risk_errors if not e.get("blocking", True)]

        if _blocking:
            first = _blocking[0]
            await self.send_json({
                "type": "error",
                "code": first["code"],
                "message": first["message"],
            })
            if self.account.get("status") not in ("Activo",):
                await self.send_json({
                    "type": "account:suspended",
                    "status": self.account["status"],
                    "reason": first["code"],
                })
            return

        # Non-blocking warnings (RETAIL exposure/DD warnings) — order still proceeds
        if _warnings:
            await self.send_json({
                "type": "risk:warning",
                "warnings": [{"code": w["code"], "message": w["message"]} for w in _warnings],
            })

        dec = step_decimals_for(sym)[1]
        px_exec = round(self.exec_price(sym, side), dec)

        commission  = self.commission_for(sym, qty, px_exec)
        new_balance = self.account["balance"] - commission

        try:
            result = await self._db_open_position_atomic(
                sym, side, qty, px_exec, sl, tp, commission, new_balance
            )
        except Exception as exc:
            log.error("[order_new] DB open failed for %s %s: %s", side, sym, exc, exc_info=True)
            await self.send_json({"type": "error", "code": "execution_failed",
                                  "message": "no_se_pudo_abrir_posicion"})
            return

        # DB committed — safe to mutate memory now
        self.account["balance"] = new_balance
        db_pos_id = result["position_id"] or self._order_seq
        self._order_seq += 1

        if self.account.get("netting_mode"):
            self._open_or_update_position(sym, side, qty, px_exec, sl, tp, position_id=db_pos_id)
        else:
            self._create_position(sym, side, qty, px_exec, sl, tp, position_id=db_pos_id)

        await self.send_json({"type":"order_ack","order_id":db_pos_id,"symbol":sym,"side":side,"qty":qty,"status":"accepted"})
        await self.send_json({"type":"order_fill","order_id":db_pos_id,"symbol":sym,"side":side,"qty":qty,"price":px_exec,
                              "commission":commission,"ts":int(time.time())})

        await self._recalc_account_and_push()
        await self.send_json({"type":"positions","items":self._positions_snapshot()})

    async def _order_update(self, data: dict):
        pid = data.get("id")
        try: pid = int(pid)
        except (ValueError, TypeError): pass
        sym = data.get("symbol", self.symbol)
        sl  = data.get("sl", None)
        tp  = data.get("tp", None)

        found = False
        if pid is not None:
            for p in self._positions:
                if str(p.get("id")) == str(pid) and p.get("symbol")==sym:
                    if sl is not None: p["sl"] = float(sl)
                    if tp is not None: p["tp"] = float(tp)
                    found = True
                    await self._db_mirror_update_sl_tp(pid, sym, p.get("sl"), p.get("tp"))
                    break

        if not found:
            log.warning("[order_update] no position matched pid=%r sym=%r — SL/TP update ignored", pid, sym)

        if found:
            await self.send_json({"type":"positions","items":self._positions_snapshot()})
        else:
            await self.send_json({"type":"warn","message":"order_update_not_found"})

    async def _order_close(self, data: dict):
        pid      = data.get("id")          # may arrive as str or int
        sym_hint = data.get("symbol", None)

        log.info("[close] received pid=%r sym_hint=%r positions_in_memory=%d ids=%s",
                 pid, sym_hint, len(self._positions),
                 [(p.get("id"), p.get("symbol"), p.get("side")) for p in self._positions])

        # Step A — find position in memory (read-only, no mutation yet)
        found_pos = None
        for p in self._positions:
            id_match  = (pid is not None) and (str(p.get("id")) == str(pid))
            sym_match = (sym_hint is None) or (p.get("symbol") == sym_hint)
            log.debug("[close] checking pos id=%r sym=%r → id_match=%s sym_match=%s",
                      p.get("id"), p.get("symbol"), id_match, sym_match)
            if id_match and sym_match and found_pos is None:
                found_pos = p

        if found_pos is None:
            log.warning("[close] NO MATCH for pid=%r sym_hint=%r — sending order_close_not_found", pid, sym_hint)
            await self.send_json({"type": "warn", "message": "order_close_not_found"})
            return

        # Step B — compute close values BEFORE any memory mutation
        sym      = found_pos["symbol"]
        dec      = step_decimals_for(sym)[1]
        close_px = round(self.close_price(sym, found_pos["side"]), dec)
        realized = self._realized_pnl_for(found_pos, close_px)
        new_balance = self.account["balance"] + realized
        remaining_floating = (
            self._unrealized_pnl_total()
            - self._unrealized_pnl_for(found_pos, close_px)
        )
        new_equity = round(new_balance + remaining_floating, 2)

        log.info("[close] MATCH pos id=%r sym=%r side=%r close_px=%s realized=%.4f",
                 found_pos["id"], sym, found_pos["side"], close_px, realized)

        # Step C — DB transaction FIRST (Phase 1B: DB-first close)
        try:
            result = await self._db_close_position_atomic(
                found_pos, close_px, "manual", realized, new_balance, new_equity
            )
        except Exception as exc:
            log.error("[close] DB close failed for pos id=%r: %s", found_pos["id"], exc, exc_info=True)
            await self.send_json({"type": "error", "code": "close_failed",
                                  "message": "no_se_pudo_cerrar_posicion"})
            return  # memory untouched — position still open

        # Step D — DB committed: safe to mutate memory now
        self._positions      = [p for p in self._positions if str(p.get("id")) != str(found_pos["id"])]
        self.account["balance"]      = result["new_balance"]
        self.account["peak_balance"] = result["new_peak"]
        self.account["status"]       = result["new_status"]
        self._track_daily_pnl(realized)

        # Step E — respond to client (same payloads as before)
        await self._recalc_account_and_push()
        log.info("[close] order closed OK. remaining positions=%d", len(self._positions))
        await self.send_json({"type": "order_close",
                              "id": found_pos["id"], "symbol": sym, "side": found_pos["side"],
                              "qty": found_pos["qty"], "avg": found_pos["avg"],
                              "close_px": close_px, "reason": "manual",
                              "realized_pnl": realized, "ts": int(time.time())})
        await self.send_json({"type": "positions", "items": self._positions_snapshot()})

    # ---------------- Risk Preview ----------------
    async def _handle_risk_preview(self, data: dict):
        sym = data.get("symbol", self.symbol)
        qty = float(data.get("qty", 0) or 0)
        if qty <= 0:
            return
        equity = self.account["balance"] + self._unrealized_pnl_total()
        margin = self._margin_used_total()
        lev = max(1, int(self.account.get("leverage", 50)))
        assessment = await self._db_evaluate_risk(sym, qty, equity, margin, lev)
        await self.send_json({"type": "risk_preview", **assessment})

    @database_sync_to_async
    def _db_evaluate_risk(self, symbol: str, lot_size: float,
                           equity: float, margin_used: float, leverage: int) -> dict:
        if not self._db_account_id:
            return {"risk_level": "LOW"}
        from .risk_engine import evaluate_position_risk
        account = TradingAccount.objects.filter(id=self._db_account_id).first()
        if not account:
            return {"risk_level": "LOW"}
        return evaluate_position_risk(account, symbol, lot_size, equity, margin_used, leverage)

    # ---------------- Cuenta / PnL ----------------
    def commission_for(self, symbol: str, qty: float, price: float) -> float:
        # Phase 6B: prefer per-lot snapshot if set (qty is already in lots).
        # Falls back to spec.commission_pct for old accounts with no snapshot.
        cpl = self.account.get("commission_per_lot", 0.0) or 0.0
        if cpl > 0:
            return round(qty * cpl, 2)
        spec = get_spec(symbol)
        notional = qty * price * spec.contract_size
        return max(0.0, notional * spec.commission_pct)

    def min_qty_for(self, symbol: str) -> float:
        return get_spec(symbol).min_lot

    def _pretrade_check(self, symbol, side, qty):
        spec = get_spec(symbol)
        if qty < spec.min_lot:
            return False, "min_qty_violation"
        if qty % spec.lot_step > spec.lot_step * 0.001:
            return False, "lot_step_violation"
        account_lev = max(1, int(self.account.get("leverage", 50)))
        lev = max(1, min(account_lev, spec.max_leverage))
        entry_px = self.exec_price(symbol, side)
        est_margin = abs(entry_px * qty * spec.contract_size) / lev
        equity = self.account["balance"] + self._unrealized_pnl_total()
        if est_margin > (equity - self._margin_used_total()):
            return False, "insufficient_margin"
        return True, "ok"

    def _open_or_update_position(self, symbol, side, qty, fill_px, sl=None, tp=None, position_id=None):
        dec = step_decimals_for(symbol)[1]
        for pos in self._positions:
            if pos["symbol"]==symbol and pos["side"]==side:
                new_qty = pos["qty"] + qty
                pos["avg"] = round(((pos["avg"]*pos["qty"])+(fill_px*qty))/new_qty, dec)
                pos["qty"] = new_qty
                if sl is not None: pos["sl"]=sl
                if tp is not None: pos["tp"]=tp
                return
        self._positions.append({"id":position_id or self._order_seq, "symbol":symbol,"side":side,
                                "qty":qty,"avg":round(fill_px,dec),"sl":sl,"tp":tp,
                                "opened_at":int(time.time())})

    def _create_position(self, symbol, side, qty, fill_px, sl=None, tp=None, position_id=None):
        dec = step_decimals_for(symbol)[1]
        self._positions.append({"id":position_id or self._order_seq, "symbol":symbol,"side":side,
                                "qty":qty,"avg":round(fill_px,dec),"sl":sl,"tp":tp,
                                "opened_at":int(time.time())})

    def _positions_snapshot(self): return [dict(p) for p in self._positions]

    def _unrealized_pnl_total(self):
        total = 0.0
        for p in self._positions:
            px = self.close_price(p["symbol"], p["side"])
            total += self._unrealized_pnl_for(p, px)
        return total

    def _unrealized_pnl_for(self, pos, close_px):
        spec = get_spec(pos["symbol"])
        if pos["side"] == "buy":
            return (close_px - pos["avg"]) * pos["qty"] * spec.contract_size
        return (pos["avg"] - close_px) * pos["qty"] * spec.contract_size

    def _realized_pnl_for(self, pos, close_price): return self._unrealized_pnl_for(pos, close_price)

    def _track_daily_pnl(self, amount: float) -> None:
        from datetime import date
        today = date.today()
        if self._daily_pnl_date != today:
            self._daily_realized_pnl = 0.0
            self._daily_pnl_date = today
        self._daily_realized_pnl += amount

    def _margin_used_total(self):
        account_lev = max(1, int(self.account.get("leverage", 50)))
        total = 0.0
        for p in self._positions:
            spec = get_spec(p["symbol"])
            lev = max(1, min(account_lev, spec.max_leverage))
            notional = abs(p["avg"] * p["qty"] * spec.contract_size)
            total += notional / lev
        return total

    async def _recalc_account_and_push(self):
        self.account["pnl_unreal"] = round(self._unrealized_pnl_total(), 2)
        self.account["margin_used"] = round(self._margin_used_total(), 2)
        self.account["equity"] = round(self.account["balance"] + self.account["pnl_unreal"], 2)
        free_margin = round(self.account["equity"] - self.account["margin_used"], 2)

        now = time.time()
        if self._db_account_id and (now - self._last_db_sync) > 1.2:
            await self._db_sync_account_balances()
            self._last_db_sync = now

        # Real-time stopout — only check if account is currently active
        if self.account.get("status") == "Activo" and self._positions:
            _acct_type = self.account.get("account_type", "CHALLENGE")
            from .risk_engine import check_equity_stopout
            if check_equity_stopout(
                equity=self.account["equity"],
                peak_balance=self.account["peak_balance"],
                tier=self.account.get("tier", "10K"),
                account_type=_acct_type,
                margin_used=self.account.get("margin_used", 0.0),
            ):
                from .models import MARGIN_ENGINE_TYPES
                if _acct_type in MARGIN_ENGINE_TYPES:
                    await self._do_retail_liquidation()
                else:
                    await self._do_stopout()
                return  # handler pushes its own account:update

        # Risk / challenge metrics
        peak = self.account["peak_balance"]
        balance = self.account["balance"]
        total_dd_pct = round((peak - balance) / peak * 100, 2) if peak > 0 else 0.0

        daily_pnl = self._daily_realized_pnl
        daily_dd_pct = round(abs(daily_pnl) / peak * 100, 2) if (peak > 0 and daily_pnl < 0) else 0.0

        margin_used = self.account["margin_used"]
        equity_val = self.account["equity"]
        margin_level = round(equity_val / margin_used * 100, 2) if margin_used > 0 else 0.0

        from .risk_engine import compute_margin_state
        _ms = compute_margin_state(equity_val, margin_used)
        used_margin_pct   = _ms["used_margin_pct"]
        maintenance_margin = _ms["maintenance_margin"]
        liquidation_distance = _ms["liquidation_distance"]

        dec = step_decimals_for(self.symbol)[1]
        bid = round(self.get_bid(self.symbol), dec)
        ask = round(self.get_ask(self.symbol), dec)
        spread = round(ask - bid, dec)

        await self.send_json({
            "type": "account:update",
            "balance": round(balance, 2),
            "equity": equity_val,
            "pnl_unreal": self.account["pnl_unreal"],
            "upnl": self.account["pnl_unreal"],
            "margin_used": margin_used,
            "free_margin": free_margin,
            "used_margin_pct": used_margin_pct,
            "maintenance_margin": maintenance_margin,
            "liquidation_distance": liquidation_distance,
            "leverage": self.account["leverage"],
            "netting_mode": bool(self.account.get("netting_mode", False)),
            "status": self.account.get("status", "Activo"),
            "account_type": self.account.get("account_type", "CHALLENGE"),
            "total_dd_pct": total_dd_pct,
            "daily_dd_pct": daily_dd_pct,
            "daily_pnl": round(daily_pnl, 2),
            "margin_level": margin_level,
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "profit_target": self.account.get("profit_target", 800.0),
            "initial_balance": self.account.get("initial_balance", self.account.get("balance", 0.0)),
            # Phase 6B — product rule info
            "product_name":       self.account.get("product_name", ""),
            "commission_per_lot": self.account.get("commission_per_lot", 0.0),
            "spread_pips":        self.account.get("spread_pips", 0.0),
            "currency":           self.account.get("currency", "USD"),
        })

    async def _do_stopout(self) -> None:
        """Close ALL open positions at current bid/ask and suspend the account."""
        log.warning("[stopout] equity=%.2f triggered for account #%s",
                    self.account["equity"], self._db_account_id)
        closed_items = []
        failed_positions = []
        now_ts = int(time.time())
        running_balance = self.account["balance"]
        total_floating_snapshot = self._unrealized_pnl_total()
        accum_floating_closed = 0.0

        for p in list(self._positions):
            sym  = p["symbol"]
            dec  = step_decimals_for(sym)[1]
            cpx  = round(self.close_price(sym, p["side"]), dec)
            realized = self._realized_pnl_for(p, cpx)
            new_balance = running_balance + realized
            fp_p = self._unrealized_pnl_for(p, cpx)
            remaining_floating = total_floating_snapshot - accum_floating_closed - fp_p
            new_equity = round(new_balance + remaining_floating, 2)
            try:
                result = await self._db_close_position_atomic(
                    p, cpx, "stopout", realized, new_balance, new_equity
                )
                running_balance = result["new_balance"]
                accum_floating_closed += fp_p
                self._track_daily_pnl(realized)
                closed_items.append({
                    "id": p["id"], "symbol": sym, "side": p["side"],
                    "qty": p["qty"], "avg": p["avg"],
                    "close_px": cpx, "reason": "stopout",
                    "realized_pnl": realized, "ts": now_ts,
                })
            except Exception as exc:
                log.error("[stopout] DB close failed pos %s: %s", p["id"], exc)
                failed_positions.append(p)

        # DB commits done — update memory, then persist suspension
        self.account["balance"] = running_balance
        self.account["equity"]  = round(running_balance, 2)
        self._positions = failed_positions

        try:
            await self._db_suspend_account("stopout")
        except Exception as exc:
            log.error("[stopout] DB suspend failed: %s", exc)

        self.account["status"]      = "Suspendido"
        self.account["pnl_unreal"]  = 0.0
        self.account["margin_used"] = 0.0

        # Notify client
        for c in closed_items:
            await self.send_json({"type": "order_close", **c})
        await self.send_json({"type": "positions", "items": []})
        await self.send_json({
            "type": "account:suspended",
            "status": "Suspendido",
            "reason": "stopout",
        })
        peak = self.account["peak_balance"]
        balance = self.account["balance"]
        total_dd_pct = round((peak - balance) / peak * 100, 2) if peak > 0 else 0.0
        daily_pnl = self._daily_realized_pnl
        daily_dd_pct = round(abs(daily_pnl) / peak * 100, 2) if (peak > 0 and daily_pnl < 0) else 0.0
        await self.send_json({
            "type": "account:update",
            "balance": round(balance, 2),
            "equity": self.account["equity"],
            "pnl_unreal": 0.0,
            "upnl": 0.0,
            "margin_used": 0.0,
            "free_margin": self.account["equity"],
            "used_margin_pct": 0.0,
            "maintenance_margin": 0.0,
            "liquidation_distance": self.account["equity"],
            "leverage": self.account["leverage"],
            "netting_mode": bool(self.account.get("netting_mode", False)),
            "status": "Suspendido",
            "account_type": self.account.get("account_type", "CHALLENGE"),
            "total_dd_pct": total_dd_pct,
            "daily_dd_pct": daily_dd_pct,
            "daily_pnl": round(daily_pnl, 2),
            "margin_level": 0.0,
            "bid": round(self.get_bid(self.symbol), step_decimals_for(self.symbol)[1]),
            "ask": round(self.get_ask(self.symbol), step_decimals_for(self.symbol)[1]),
            "spread": 0.0,
            "profit_target": self.account.get("profit_target", 800.0),
            "initial_balance": self.account.get("initial_balance", self.account.get("balance", 0.0)),
        })

    async def _do_retail_liquidation(self) -> None:
        """RETAIL margin call — close all positions, account stays ACTIVE.
        Triggers when margin_level < 50%. Unlike _do_stopout, no suspension."""
        log.warning("[margin_call] margin_level<50%% equity=%.2f margin=%.2f account #%s",
                    self.account["equity"], self.account.get("margin_used", 0.0),
                    self._db_account_id)
        closed_items = []
        failed_positions = []
        now_ts = int(time.time())
        running_balance = self.account["balance"]
        total_floating_snapshot = self._unrealized_pnl_total()
        accum_floating_closed = 0.0

        for p in list(self._positions):
            sym  = p["symbol"]
            dec  = step_decimals_for(sym)[1]
            cpx  = round(self.close_price(sym, p["side"]), dec)
            realized = self._realized_pnl_for(p, cpx)
            new_balance = running_balance + realized
            fp_p = self._unrealized_pnl_for(p, cpx)
            remaining_floating = total_floating_snapshot - accum_floating_closed - fp_p
            new_equity = round(new_balance + remaining_floating, 2)
            try:
                result = await self._db_close_position_atomic(
                    p, cpx, "margin_call", realized, new_balance, new_equity
                )
                running_balance = result["new_balance"]
                accum_floating_closed += fp_p
                self._track_daily_pnl(realized)
                closed_items.append({
                    "id": p["id"], "symbol": sym, "side": p["side"],
                    "qty": p["qty"], "avg": p["avg"],
                    "close_px": cpx, "reason": "margin_call",
                    "realized_pnl": realized, "ts": now_ts,
                })
            except Exception as exc:
                log.error("[margin_call] DB close failed pos %s: %s", p["id"], exc)
                failed_positions.append(p)

        # DB commits done — update memory
        self.account["balance"] = running_balance
        self._positions = failed_positions
        self.account["pnl_unreal"]  = 0.0
        self.account["margin_used"] = 0.0
        self.account["equity"]      = round(self.account["balance"], 2)

        for c in closed_items:
            await self.send_json({"type": "order_close", **c})
        await self.send_json({"type": "positions", "items": []})
        await self.send_json({
            "type": "account:margin_call",
            "reason": "margin_level_below_50pct",
            "balance": round(self.account["balance"], 2),
        })
        dec = step_decimals_for(self.symbol)[1]
        balance = self.account["balance"]
        await self.send_json({
            "type": "account:update",
            "balance": round(balance, 2),
            "equity": self.account["equity"],
            "pnl_unreal": 0.0, "upnl": 0.0,
            "margin_used": 0.0, "free_margin": self.account["equity"],
            "used_margin_pct": 0.0, "maintenance_margin": 0.0,
            "liquidation_distance": self.account["equity"],
            "leverage": self.account["leverage"],
            "netting_mode": bool(self.account.get("netting_mode", False)),
            "status": self.account.get("status", "Activo"),  # stays Active
            "account_type": "RETAIL",
            "total_dd_pct": 0.0, "daily_dd_pct": 0.0,
            "daily_pnl": round(self._daily_realized_pnl, 2),
            "margin_level": 0.0,
            "bid": round(self.get_bid(self.symbol), dec),
            "ask": round(self.get_ask(self.symbol), dec),
            "spread": 0.0,
            "profit_target": self.account.get("profit_target", 0.0),
            "initial_balance": self.account.get("initial_balance", balance),
        })

    async def _check_tp_sl(self, symbol: str, bid: float, ask: float):
        dec = step_decimals_for(symbol)[1]
        remaining, closed = [], []
        now = int(time.time())
        running_balance = self.account["balance"]
        total_floating_snapshot = self._unrealized_pnl_total()
        accum_floating_closed = 0.0

        for p in self._positions:
            if p["symbol"] != symbol:
                remaining.append(p); continue

            side = p["side"]; sl = p.get("sl"); tp = p.get("tp")
            # BUY: triggers checked against BID (the price you'd exit at)
            # SELL: triggers checked against ASK
            trigger_px = bid if side == "buy" else ask
            fill_px    = bid if side == "buy" else ask  # same: close at bid/ask

            trail = p.get("trail_dist")
            if trail and trail > 0:
                if side == "buy":
                    p["best"] = max(p.get("best", p["avg"]), bid)
                    p["sl"] = round(p["best"] - trail, dec)
                    sl = p["sl"]
                else:
                    p["best"] = min(p.get("best", p["avg"]), ask)
                    p["sl"] = round(p["best"] + trail, dec)
                    sl = p["sl"]

            sl_hit = sl is not None and ((side=="buy" and trigger_px<=sl) or (side=="sell" and trigger_px>=sl))
            tp_hit = tp is not None and ((side=="buy" and trigger_px>=tp) or (side=="sell" and trigger_px<=tp))

            if sl_hit or tp_hit:
                close_px    = round(fill_px, dec)
                realized    = self._realized_pnl_for(p, close_px)
                new_balance = running_balance + realized
                fp_p        = self._unrealized_pnl_for(p, close_px)
                remaining_floating = total_floating_snapshot - accum_floating_closed - fp_p
                new_equity  = round(new_balance + remaining_floating, 2)
                reason = "tp" if tp_hit else "sl"
                try:
                    result = await self._db_close_position_atomic(
                        p, close_px, reason, realized, new_balance, new_equity
                    )
                    running_balance = result["new_balance"]
                    accum_floating_closed += fp_p
                    closed.append({"id":p["id"],"symbol":symbol,"side":side,"qty":p["qty"],"avg":p["avg"],
                                   "close_px":close_px,"reason":reason,
                                   "realized_pnl":realized,"ts":now})
                except Exception as exc:
                    log.error("[tp_sl] db close FAILED pos id=%r: %s", p["id"], exc, exc_info=True)
                    remaining.append(p)
            else:
                remaining.append(p)

        if closed:
            self.account["balance"] = running_balance
            self._track_daily_pnl(sum(c["realized_pnl"] for c in closed))
            self._positions = remaining
            await self._recalc_account_and_push()
            for c in closed: await self.send_json({"type":"order_close", **c})
            await self.send_json({"type":"positions","items":self._positions_snapshot()})

    # ---------------- DB helpers (best-effort) ----------------
    async def send_positions_snapshot(self):
        items = await self._db_fetch_open_positions()
        self._positions = [
            {
                "id": it["id"], "symbol": it["symbol"], "side": it["side"].lower(),
                "qty": float(it["qty"]), "avg": float(it["avg_price"]),
                "sl": it.get("sl"), "tp": it.get("tp"),
                "opened_at": it.get("opened_ts", int(time.time())),
            }
            for it in items
        ]
        log.info("[positions_snapshot] sending %d position(s) ids=%s",
                 len(self._positions), [p["id"] for p in self._positions])
        await self.send_json({"type": "positions", "items": self._positions_snapshot()})

    async def _maybe_hydrate_from_db(self):
        if not self._db_account_id:
            log.warning("[hydrate] SKIPPED — db_account_id is None")
            return
        log.info("[hydrate] loading account #%s from DB", self._db_account_id)
        acc = await self._db_read_account(self._db_account_id)
        if not acc:
            log.warning("[hydrate] account #%s not found in DB", self._db_account_id)
            return

        self.account["balance"]      = float(acc.get("balance",      self.account["balance"]))
        self.account["equity"]       = float(acc.get("equity",       self.account["equity"]))
        self.account["peak_balance"] = float(acc.get("peak_balance", self.account["balance"]))
        self.account["leverage"]     = int(acc.get("leverage",       self.account["leverage"]))
        self.account["netting_mode"] = bool(acc.get("netting_mode",  self.account["netting_mode"]))
        self.account["status"]          = acc.get("status", "Activo")
        self.account["tier"]            = acc.get("tier", "")
        self.account["account_type"]    = acc.get("account_type", "CHALLENGE")
        self.account["profit_target"]   = float(acc.get("profit_target") or 0.0)
        # Use the stored initial_balance from DB; fall back to current balance, never to a tier dict.
        self.account["initial_balance"] = float(
            acc.get("initial_balance") or self.account["balance"]
        )
        # Phase 6B — product rule snapshots (None = not set, fallback to spec/default)
        self.account["product_name"]      = acc.get("product_name", "")
        self.account["commission_per_lot"] = acc.get("commission_per_lot", 0.0)
        self.account["spread_pips"]        = acc.get("spread_pips", 0.0)
        self.account["allowed_symbols"]    = acc.get("allowed_symbols", None)
        self.account["max_lot_size"]       = acc.get("max_lot_size", None)
        self.account["margin_call_level"]  = acc.get("margin_call_level", 100.0)
        self.account["stopout_level"]      = acc.get("stopout_level", 50.0)
        log.info("[hydrate] balance=%.2f equity=%.2f status=%s tier=%s product=%r comm_per_lot=%.2f",
                 self.account["balance"], self.account["equity"],
                 self.account["status"], self.account["tier"],
                 self.account["product_name"], self.account["commission_per_lot"])

        items = await self._db_fetch_open_positions()
        self._positions = []
        for it in items:
            self._positions.append({
                "id":it["id"], "symbol":it["symbol"], "side":it["side"].lower(),
                "qty":float(it["qty"]), "avg":float(it["avg_price"]),
                "sl":it.get("sl"), "tp":it.get("tp"),
                "opened_at":it.get("opened_ts", int(time.time())),
            })
        if self._positions:
            self._order_seq = max(int(p["id"]) for p in self._positions) + 1
        else:
            self._order_seq = 1
        log.info("[hydrate] loaded %d open position(s): %s — _order_seq set to %d",
                 len(self._positions), [(p["id"], p["symbol"], p["side"]) for p in self._positions],
                 self._order_seq)

        daily_pnl = await self._db_fetch_daily_pnl()
        self._daily_realized_pnl = daily_pnl
        from datetime import date as _date
        self._daily_pnl_date = _date.today()
        log.info("[hydrate] daily_realized_pnl=%.2f for %s", self._daily_realized_pnl, self._daily_pnl_date)

    @database_sync_to_async
    def _db_suspend_account(self, reason: str) -> None:
        if not self._db_account_id:
            return
        from django.db import transaction
        from decimal import Decimal
        with transaction.atomic():
            account = (
                TradingAccount.objects.select_for_update()
                .filter(id=self._db_account_id)
                .first()
            )
            if account:
                account.status  = "Suspendido"
                account.balance = Decimal(str(self.account["balance"]))
                account.equity  = Decimal(str(self.account["equity"]))
                account.save(update_fields=["status", "balance", "equity"])
                LedgerEntry.objects.create(
                    account=account,
                    event_type=LedgerEntry.EV_ADJUST,
                    amount=Decimal("0"),
                    balance_after=account.balance,
                    meta={"reason": reason},
                )

    @database_sync_to_async
    def _db_get_account_for_user(self, acc_id:int, user_id:int):
        try:
            obj = TradingAccount.objects.get(id=acc_id, user_id=user_id)
            return {"id":obj.id}
        except TradingAccount.DoesNotExist:
            return None

    @database_sync_to_async
    def _db_get_latest_account_for_user(self, user_id):
        if not user_id:
            return None
        obj = (TradingAccount.objects
               .filter(user_id=user_id, status="Activo")
               .order_by("-id")
               .first())
        return {"id": obj.id} if obj else None

    @database_sync_to_async
    def _db_read_account(self, acc_id: int):
        try:
            obj = TradingAccount.objects.get(id=acc_id)
            return {
                "id":              obj.id,
                "account_type":    obj.account_type,
                "balance":         obj.balance,
                "equity":          obj.equity,
                "peak_balance":    obj.peak_balance,
                "initial_balance": obj.initial_balance,
                "leverage":        getattr(obj, "leverage", 50),
                "netting_mode":    getattr(obj, "netting_mode", False),
                "status":          obj.status,
                "tier":            obj.tier or "",
                "profit_target":   float(obj.profit_target) if obj.profit_target is not None else 0.0,
                # Phase 6B — product snapshots
                "product_name":          obj.product_name_snapshot or "",
                "commission_per_lot":    float(obj.commission_per_lot_snapshot or 0),
                "spread_pips":           float(obj.spread_pips_snapshot or 0),
                "allowed_symbols":       obj.allowed_symbols_snapshot,
                "max_lot_size":          float(obj.max_lot_size_snapshot) if obj.max_lot_size_snapshot is not None else None,
                "margin_call_level":     float(obj.margin_call_level_snapshot or 100),
                "stopout_level":         float(obj.stopout_level_snapshot or 50),
            }
        except TradingAccount.DoesNotExist:
            return None

    @database_sync_to_async
    def _db_validate_order_risk(self, lot_size: float, open_positions_count: int,
                                symbol: str = "") -> list[dict]:
        """Returns list of error dicts. Empty = allowed. Creates violations on hard breaches."""
        if not self._db_account_id:
            return []
        from django.db import transaction
        from .risk_engine import validate_order_risk
        with transaction.atomic():
            account = (
                TradingAccount.objects.select_for_update()
                .filter(id=self._db_account_id)
                .first()
            )
            if not account:
                return [{"code": "account_not_found", "message": "Cuenta no encontrada"}]
            errors = validate_order_risk(account, lot_size, open_positions_count, symbol)
            # Sync status back to in-memory if account was suspended
            if account.status != self.account.get("status"):
                self.account["status"] = account.status
            return errors

    @database_sync_to_async
    def _db_fetch_open_positions(self):
        if not self._db_account_id: return []
        out=[]
        qs = Position.objects.filter(account_id=self._db_account_id)
        for p in qs:
            out.append({
                "id": p.id, "symbol": p.symbol, "side": p.side,
                "qty": float(p.qty), "avg_price": float(p.avg_price),
                "sl": float(p.sl) if p.sl is not None else None,
                "tp": float(p.tp) if p.tp is not None else None,
                "opened_ts": int(p.opened_at.timestamp()),
            })
        return out

    @database_sync_to_async
    def _db_fetch_daily_pnl(self) -> float:
        if not self._db_account_id:
            return 0.0
        from django.utils import timezone
        from django.db.models import Sum
        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        result = (
            LedgerEntry.objects
            .filter(
                account_id=self._db_account_id,
                event_type=LedgerEntry.EV_REALIZED,
                created_at__gte=today_start,
            )
            .aggregate(total=Sum("amount"))["total"]
        )
        return float(result or 0)

    @database_sync_to_async
    def _db_sync_account_balances(self):
        if not self._db_account_id: return
        TradingAccount.objects.filter(id=self._db_account_id).update(
            balance=self.account["balance"], equity=self.account["equity"]
        )

    @database_sync_to_async
    def _db_mirror_open_or_update(self, order_id, symbol, side, qty, price, sl, tp, commission):
        # Deprecated — superseded by _db_open_position_atomic (Phase 1A). No longer called.
        if not self._db_account_id: return
        from decimal import Decimal
        with transaction.atomic():
            if commission and commission>0:
                LedgerEntry.objects.create(
                    account_id=self._db_account_id, event_type=LedgerEntry.EV_COMMISSION,
                    amount=Decimal(-abs(commission)), balance_after=Decimal(self.account["balance"]),
                    meta={"symbol":symbol,"side":side,"client_pos_id":order_id},
                )
            Position.objects.create(
                account_id=self._db_account_id, symbol=symbol, side=side,
                qty=Decimal(qty), avg_price=Decimal(price),
                **({"sl":Decimal(sl)} if sl is not None else {}),
                **({"tp":Decimal(tp)} if tp is not None else {}),
                external_id=str(order_id),
            )

    @database_sync_to_async
    def _db_open_position_atomic(self, symbol: str, side: str, qty: float, price: float,
                                  sl, tp, commission: float, new_balance: float) -> dict:
        """DB-first order open (Phase 1A).

        Atomically: create/merge Position, record commission LedgerEntry, update
        TradingAccount.balance — all in one transaction before any memory mutation.

        Returns {"position_id": int, "merged": bool}.
        If _db_account_id is None (demo session) returns {"position_id": None, "merged": False}
        so the caller falls back to _order_seq as a local id.
        """
        if not self._db_account_id:
            return {"position_id": None, "merged": False}
        from decimal import Decimal
        with transaction.atomic():
            # Netting: look for an existing position on same symbol+side and lock it.
            existing = None
            if self.account.get("netting_mode"):
                existing = (
                    Position.objects
                    .select_for_update()
                    .filter(
                        account_id=self._db_account_id,
                        symbol=symbol,
                        side=side.upper(),
                    )
                    .first()
                )

            if existing:
                # Merge into the existing row — weighted average price.
                new_qty = existing.qty + Decimal(str(qty))
                new_avg = (
                    existing.avg_price * existing.qty
                    + Decimal(str(price)) * Decimal(str(qty))
                ) / new_qty
                existing.avg_price = new_avg.quantize(Decimal("0.000001"))
                existing.qty = new_qty
                if sl is not None:
                    existing.sl = Decimal(str(sl))
                if tp is not None:
                    existing.tp = Decimal(str(tp))
                existing.save(update_fields=["qty", "avg_price", "sl", "tp"])
                position_id = existing.id
                merged = True
            else:
                pos = Position.objects.create(
                    account_id=self._db_account_id,
                    symbol=symbol,
                    side=side.upper(),
                    qty=Decimal(str(qty)),
                    avg_price=Decimal(str(price)),
                    sl=Decimal(str(sl)) if sl is not None else None,
                    tp=Decimal(str(tp)) if tp is not None else None,
                )
                position_id = pos.id
                merged = False

            if commission and commission > 0:
                trader_ledger = LedgerEntry.objects.create(
                    account_id=self._db_account_id,
                    event_type=LedgerEntry.EV_COMMISSION,
                    amount=Decimal(str(-abs(commission))),
                    balance_after=Decimal(str(new_balance)),
                    meta={"symbol": symbol, "side": side, "db_pos_id": position_id},
                )
                try:
                    BrokerLedger.objects.create(
                        revenue_type=BrokerLedger.REV_COMMISSION,
                        amount=Decimal(str(abs(commission))),
                        source_account_id=self._db_account_id,
                        source_ledger=trader_ledger,
                        symbol=symbol,
                        meta={"side": side, "db_pos_id": position_id},
                    )
                except Exception as _bl_exc:
                    log.warning("[broker_ledger] commission insert failed pos=%s: %s", position_id, _bl_exc)

            # BrokerLedger SPREAD — revenue earned at open (half-spread × qty × contract_size).
            # Nested savepoint: spread revenue failure never rolls back the trader transaction.
            _spread_cfg = _get_spread_config(symbol)
            if _spread_cfg is not None and _spread_cfg.enabled:
                try:
                    with transaction.atomic():
                        _spread_rev = calculate_spread_revenue(
                            symbol, float(qty), float(_spread_cfg.spread_pips)
                        )
                        if _spread_rev > 0:
                            BrokerLedger.objects.create(
                                revenue_type=BrokerLedger.REV_SPREAD,
                                amount=Decimal(str(_spread_rev)),
                                source_account_id=self._db_account_id,
                                symbol=symbol,
                                meta={"side": side, "db_pos_id": position_id, "spread_pips": float(_spread_cfg.spread_pips)},
                            )
                except Exception as _sp_exc:
                    log.warning("[broker_ledger] spread insert failed pos=%s: %s", position_id, _sp_exc)

            TradingAccount.objects.filter(id=self._db_account_id).update(
                balance=Decimal(str(new_balance))
            )

        log.info("[db_open] pos_id=%s symbol=%s side=%s qty=%s merged=%s balance=%.2f",
                 position_id, symbol, side, qty, merged, new_balance)
        return {"position_id": position_id, "merged": merged}

    @database_sync_to_async
    def _db_close_position_atomic(self, pos_mem: dict, close_px: float, reason: str,
                                   realized_pnl: float, new_balance: float,
                                   new_equity: float) -> dict:
        """DB-first order close (Phase 1B).

        Atomically: find+lock Position, create Trade, record LedgerEntry EV_REALIZED,
        delete Position, update TradingAccount balance/equity, run risk+intelligence engines.
        All committed before any memory mutation in the caller.

        Returns final DB state dict. Raises on failure — caller leaves memory untouched.
        """
        if not self._db_account_id:
            # Demo/anonymous session — skip DB, return current values for memory mutation.
            return {
                "new_balance": new_balance,
                "new_equity":  new_equity,
                "new_status":  self.account.get("status", "Activo"),
                "new_peak":    self.account.get("peak_balance", new_balance),
                "violations":  [],
                "trade_id":    None,
            }
        from decimal import Decimal
        with transaction.atomic():
            # 1. Find and lock the Position row (prevents concurrent duplicate closes).
            pos = (
                Position.objects
                .select_for_update()
                .filter(id=pos_mem["id"], account_id=self._db_account_id)
                .first()
            )
            if pos is None:
                log.info("[db_close] pos %r already closed by concurrent close — skipping", pos_mem["id"])
                return {
                    "new_balance":    new_balance,
                    "new_equity":     new_equity,
                    "new_status":     self.account.get("status", "Activo"),
                    "new_peak":       self.account.get("peak_balance", new_balance),
                    "violations":     [],
                    "trade_id":       None,
                    "already_closed": True,
                }

            # 2. Create Trade record.
            trade_type = str(pos_mem.get("side", "")).upper()
            if trade_type not in ("BUY", "SELL"):
                trade_type = "BUY"
            trade = Trade.objects.create(
                account_id=self._db_account_id,
                symbol=pos_mem["symbol"],
                trade_type=trade_type,
                lot_size=Decimal(str(pos_mem["qty"])),
                entry_price=Decimal(str(pos_mem["avg"])),
                exit_price=Decimal(str(close_px)),
                stop_loss=Decimal(str(pos_mem["sl"])) if pos_mem.get("sl") is not None else None,
                take_profit=Decimal(str(pos_mem["tp"])) if pos_mem.get("tp") is not None else None,
                profit_loss=Decimal(str(realized_pnl)),
                opened_at=datetime.utcfromtimestamp(int(pos_mem.get("opened_at", time.time()))),
                closed_at=datetime.utcnow(),
            )

            # 3. Record LedgerEntry EV_REALIZED (balance_after = post-close balance).
            LedgerEntry.objects.create(
                account_id=self._db_account_id,
                event_type=LedgerEntry.EV_REALIZED,
                amount=Decimal(str(realized_pnl)),
                balance_after=Decimal(str(new_balance)),
                meta={"symbol": pos_mem["symbol"], "side": pos_mem["side"],
                      "reason": reason, "trade_id": trade.id},
            )

            # 4. Delete Position if it existed in DB.
            if pos:
                pos.delete()

            # 5. Update TradingAccount balance + equity (with lock for consistency).
            account = (
                TradingAccount.objects
                .select_for_update()
                .filter(id=self._db_account_id)
                .first()
            )
            if account:
                account.balance = Decimal(str(new_balance))
                account.equity  = Decimal(str(new_equity))
                account.save(update_fields=["balance", "equity"])

                # 6. Risk engine — challenge/funded compliance checks.
                from .risk_engine import check_and_enforce_risk
                violations = check_and_enforce_risk(account)
                if violations:
                    log.warning("[db_close] risk violations account #%s: %s",
                                self._db_account_id, [v.violation_type for v in violations])

                # 7. Intelligence engine — behavioral classification.
                from .intelligence_engine import update_intelligence
                update_intelligence(account)

                final_status = account.status
                final_peak   = float(account.peak_balance)
            else:
                violations   = []
                final_status = self.account.get("status", "Activo")
                final_peak   = self.account.get("peak_balance", new_balance)

        log.info("[db_close] OK pos_id=%r trade_id=%r realized=%.4f balance=%.2f status=%s",
                 pos_mem["id"], trade.id, realized_pnl, new_balance, final_status)
        return {
            "new_balance": float(new_balance),
            "new_equity":  float(new_equity),
            "new_status":  final_status,
            "new_peak":    final_peak,
            "violations":  [v.violation_type for v in violations],
            "trade_id":    trade.id,
        }

    @database_sync_to_async
    def _db_mirror_update_sl_tp(self, pos_id, symbol, sl, tp):
        if not self._db_account_id or not pos_id: return
        try:
            pos = Position.objects.get(id=pos_id, account_id=self._db_account_id)
        except Position.DoesNotExist:
            log.warning("[db_update_sl_tp] no DB Position for id=%r sym=%r — SL/TP DB update skipped", pos_id, symbol)
            return
        changed=False
        from decimal import Decimal
        if sl is not None: pos.sl = Decimal(sl); changed=True
        if tp is not None: pos.tp = Decimal(tp); changed=True
        if changed: pos.save()

    @database_sync_to_async
    def _db_mirror_close_position(self, pos_mem, close_px, reason, realized_pnl):
        # Deprecated for manual close — superseded by _db_close_position_atomic (Phase 1B).
        # Still called by _check_tp_sl, _do_stopout, _do_retail_liquidation (best-effort paths).
        if not self._db_account_id:
            log.warning("[db_close] SKIPPED — db_account_id is None")
            return
        from decimal import Decimal
        log.info("[db_close] starting for pos id=%r sym=%r side=%r close_px=%s realized=%.4f reason=%s",
                 pos_mem.get("id"), pos_mem.get("symbol"), pos_mem.get("side"),
                 close_px, realized_pnl, reason)
        with transaction.atomic():
            # look up the DB Position by in-memory id first, then by symbol+side
            pos = Position.objects.filter(id=pos_mem["id"], account_id=self._db_account_id).first()
            if pos:
                log.info("[db_close] found Position by id=%r", pos_mem["id"])
            else:
                log.warning("[db_close] no DB Position for id=%r — Trade will still be created", pos_mem["id"])

            # Normalise side to uppercase for trade_type field
            raw_side = str(pos_mem.get("side", "")).upper()
            trade_type = raw_side if raw_side in ("BUY", "SELL") else ("BUY" if raw_side == "BUY" else "SELL")

            log.info("[db_close] creating Trade: sym=%s type=%s qty=%s entry=%s exit=%s pnl=%s",
                     pos_mem["symbol"], trade_type, pos_mem["qty"], pos_mem["avg"], close_px, realized_pnl)
            trade = Trade.objects.create(
                account_id=self._db_account_id,
                symbol=pos_mem["symbol"],
                trade_type=trade_type,
                lot_size=Decimal(str(pos_mem["qty"])),
                entry_price=Decimal(str(pos_mem["avg"])),
                exit_price=Decimal(str(close_px)),
                stop_loss=Decimal(str(pos_mem["sl"])) if pos_mem.get("sl") is not None else None,
                take_profit=Decimal(str(pos_mem["tp"])) if pos_mem.get("tp") is not None else None,
                profit_loss=Decimal(str(realized_pnl)),
                opened_at=datetime.utcfromtimestamp(int(pos_mem.get("opened_at", time.time()))),
                closed_at=datetime.utcnow(),
            )
            log.info("[db_close] Trade created id=%r", trade.id)

            ledger = LedgerEntry.objects.create(
                account_id=self._db_account_id,
                event_type=LedgerEntry.EV_REALIZED,
                amount=Decimal(str(realized_pnl)),
                balance_after=Decimal(str(self.account["balance"])),
                meta={"symbol": pos_mem["symbol"], "side": pos_mem["side"], "reason": reason},
            )
            log.info("[db_close] LedgerEntry created id=%r", ledger.id)

            if pos:
                pos.delete()
                log.info("[db_close] Position deleted")

            account = (
                TradingAccount.objects.select_for_update()
                .filter(id=self._db_account_id)
                .first()
            )
            if account:
                account.balance = Decimal(str(self.account["balance"]))
                account.equity  = Decimal(str(self.account["equity"]))
                account.save(update_fields=["balance", "equity"])
                log.info("[db_close] TradingAccount balance synced to %.2f", self.account["balance"])

                # Risk engine — compliance violations + drawdown
                from .risk_engine import check_and_enforce_risk
                violations = check_and_enforce_risk(account)
                if violations:
                    log.warning(
                        "[risk] account #%s suspended: %s",
                        self._db_account_id,
                        [v.violation_type for v in violations],
                    )
                # Sync DB state back to memory (status + peak_balance updated by risk engine)
                self.account["status"]       = account.status
                self.account["peak_balance"] = float(account.peak_balance)

                # Intelligence engine — behavioral analysis + classification + routing
                from .intelligence_engine import update_intelligence
                update_intelligence(account)

    # ---------------- Observability ----------------
    async def _ws_counter(self, delta: int) -> None:
        """Increment (+1) or decrement (-1) the active WS connections counter in Redis."""
        try:
            from django.conf import settings as _s
            import asyncio
            from .observability import ws_incr, ws_decr
            url = getattr(_s, "REDIS_URL", "") or "redis://127.0.0.1:6379/0"
            loop = asyncio.get_event_loop()
            if delta > 0:
                await loop.run_in_executor(None, ws_incr, url)
            else:
                await loop.run_in_executor(None, ws_decr, url)
        except Exception:
            pass  # counter failure must never break WS

    # ---------------- Util: enviar JSON ----------------
    async def send_json(self, payload: dict):
        await self.send(text_data=json.dumps(payload))