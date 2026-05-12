# simulator/consumers.py
import os, json, asyncio, random, time, logging
from datetime import datetime
from urllib.parse import parse_qs

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.db import transaction

from market_data.feeds import get_feed_manager
from .models import TradingAccount, Position, Trade, LedgerEntry

log = logging.getLogger("simulator.ws")

FINNHUB_API_KEY = (os.getenv("FINNHUB_API_KEY", "") or "").strip()
DEFAULT_TICK_INTERVAL = float(os.getenv("PRICE_TICK_INTERVAL", "1.0"))

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
def step_decimals_for(symbol: str):
    if symbol in ("BTCUSD","ETHUSD"): return (0.01, 2)
    if symbol.endswith("/JPY"):        return (0.001, 3)
    if "/" in symbol:                  return (0.00001, 5)
    return (0.00001, 5)

def spread_for(symbol: str):
    if symbol == "BTCUSD": return 0.3  # ~30 ticks de 0.01
    if symbol == "ETHUSD": return 0.1
    if symbol.endswith("/JPY"): return 0.004
    if "/" in symbol: return 0.00002
    return 0.00002

def drift_for(symbol: str):
    if symbol == "BTCUSD": return 12.0
    if symbol == "ETHUSD": return 2.0
    return 0.0008

def base_price_for(symbol: str):
    return {
        "EUR/USD": 1.17000,
        "GBP/USD": 1.30000,
        "USD/JPY": 155.000,
        "AUD/USD": 0.68000,
        "BTCUSD": 68000.0,
        "ETHUSD": 3400.0,
    }.get(symbol, 1.17000)


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

        if is_auth and q_account:
            acc = await self._db_get_account_for_user(q_account, user.id)
            if acc:
                self._db_account_id = acc["id"]
                log.info("[connect] db_account_id=%s (from URL param)", self._db_account_id)

        # Fallback 1: account_id stored in Django session by login_view
        if is_auth and not self._db_account_id:
            session = self.scope.get("session", {})
            sess_acc_id = session.get("account_id")
            log.info("[connect] session account_id=%s", sess_acc_id)
            if sess_acc_id:
                acc = await self._db_get_account_for_user(int(sess_acc_id), user.id)
                if acc:
                    self._db_account_id = acc["id"]
                    log.info("[connect] db_account_id=%s (from session)", self._db_account_id)

        # Fallback 2: most-recent active account for this user
        if is_auth and not self._db_account_id:
            acc = await self._db_get_latest_account_for_user(getattr(user, "id", None))
            if acc:
                self._db_account_id = acc["id"]
                log.info("[connect] db_account_id=%s (from DB fallback)", self._db_account_id)

        if not self._db_account_id:
            log.warning("[connect] NO db_account_id resolved — all DB writes will be skipped")

        await self.accept()

        # --- Estado inicial (memoria) ---
        self.symbol = "EUR/USD"
        self.timeframe = normalize_tf(q_tf_raw or "1m")
        self._price_state = {}   # ultima referencia por símbolo
        self._order_seq = 1
        self._order_timestamps = []  # rate limiting: timestamps of recent _order_new calls
        self._positions = []
        self._agg = {}
        self._last_bar_time = {}

        self.account = {
            "balance": 10000.0,
            "equity": 10000.0,
            "pnl_unreal": 0.0,
            "margin_used": 0.0,
            "leverage": 50,
            "netting_mode": False,
        }

        await self._maybe_hydrate_from_db()

        # Shared feed subscription
        self._feed = get_feed_manager()
        # Seed local price state from feed's last known price so history aligns
        self._price_state[self.symbol] = self._feed.last_price(self.symbol)
        await self._feed.subscribe(self.symbol, self.channel_layer, self.channel_name)

        # Heartbeat — closes stale connections after 90 s of client silence
        self._last_msg_ts = time.time()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        await self.send_positions_snapshot()
        await self._recalc_account_and_push()
        await self.send_json({"type":"ack","action":"connected",
                              "timeframe":self.timeframe,"tf_sec":tf_seconds(self.timeframe)})

    async def disconnect(self, close_code):
        # Cancel heartbeat
        hb = getattr(self, "_heartbeat_task", None)
        if hb and not hb.done():
            hb.cancel()
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
            old_sym = self.symbol
            if new_sym != old_sym:
                await self._feed.unsubscribe(old_sym, self.channel_layer, self.channel_name)
                self.symbol = new_sym
                self._reset_agg(new_sym)
                self._price_state[new_sym] = self._feed.last_price(new_sym)
                await self._feed.subscribe(new_sym, self.channel_layer, self.channel_name)
            hist = self.generate_history(new_sym, self.timeframe, bars=240)
            await self.send_json({"type": "history", "symbol": new_sym, "data": hist})
            await self.send_json({"type": "ack", "action": "symbol_changed", "symbol": new_sym})
            await self.send_json({"type": "positions", "items": self._positions_snapshot()})

        elif act == "change_timeframe":
            tf = normalize_tf(data.get("timeframe", self.timeframe))
            self.timeframe = tf
            self._reset_agg(self.symbol)
            await self.send_json({"type":"ack","action":"change_timeframe","timeframe":tf,"tf_sec":tf_seconds(tf)})

        elif act == "load_history":
            sym = data.get("symbol", self.symbol)
            tf  = normalize_tf(data.get("timeframe", self.timeframe))
            hist = self.generate_history(sym, tf, bars=240)
            await self.send_json({"type":"history","symbol":sym,"data":hist})

        elif act == "account:get":
            await self._recalc_account_and_push()

        elif act == "order:mode":
            nm = data.get("netting_mode", None)
            if isinstance(nm, bool):
                self.account["netting_mode"] = nm
                await self.send_json({"type":"info","message":f"netting_mode={nm}"})

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

    async def price_tick(self, event: dict):
        """Receives broadcast ticks from FeedManager via channel layer group."""
        symbol = event.get("symbol")
        if symbol != self.symbol:
            return
        bid = event["bid"]
        ask = event["ask"]
        mid = event["mid"]
        ts  = event["time"]

        self.set_state(symbol, mid)
        await self.send_json({"type": "tick", "symbol": symbol, "bid": bid, "ask": ask, "time": ts})
        await self._on_tick(symbol, mid, volume=0.0, ts=ts)
        await self._check_tp_sl(symbol, mid)
        await self._recalc_account_and_push()

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
        if ts is None: ts = int(datetime.utcnow().timestamp())
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
    def generate_history(self, symbol, timeframe, bars=200):
        base = self._price_state.get(symbol, base_price_for(symbol))
        step, dec = step_decimals_for(symbol)
        d = drift_for(symbol)
        now = int(datetime.utcnow().timestamp())
        tf_sec = tf_seconds(timeframe)

        series, price = [], base
        rnd = random.Random(symbol+timeframe)
        for i in range(bars, 0, -1):
            ts = now - i * tf_sec
            o = price + (rnd.random()-0.5)*d
            c = o     + (rnd.random()-0.5)*d
            h = max(o,c) + abs(rnd.random()-0.5)*d*0.6
            l = min(o,c) - abs(rnd.random()-0.5)*d*0.6
            price = c
            series.append({"time":ts,"open":round(o,dec),"high":round(h,dec),
                           "low":round(l,dec),"close":round(c,dec)})
        self.set_state(symbol, price)
        return series

    # ---------------- Estado de precio ----------------
    def ensure_state(self, symbol):
        if symbol not in self._price_state:
            self._price_state[symbol] = base_price_for(symbol)
        return self._price_state[symbol]

    def set_state(self, symbol, value):
        self._price_state[symbol] = float(value)

    # ---------------- Órdenes / Cuenta ----------------
    async def _order_new(self, data: dict):
        sym  = data.get("symbol", self.symbol)
        side = str(data.get("side","")).lower()   # 'buy' | 'sell'  (in-memory stays lowercase)
        qty  = float(data.get("qty",0) or 0)
        sl   = data.get("sl")
        tp   = data.get("tp")

        # Rate limit: max 10 new orders per 10 seconds per connection
        now = time.time()
        self._order_timestamps = [t for t in self._order_timestamps if now - t < 10]
        if len(self._order_timestamps) >= 10:
            await self.send_json({"type": "error", "code": "rate_limited", "message": "demasiadas_ordenes"})
            return
        self._order_timestamps.append(now)

        if side not in ("buy","sell") or qty <= 0:
            await self.send_json({"type":"error","code":"invalid_order","message":"orden_invalida"})
            return

        ok, reason = self._pretrade_check(sym, side, qty)
        if not ok:
            await self.send_json({"type":"error","code":reason,"message":reason})
            await self._recalc_account_and_push()
            await self.send_json({"type":"positions","items":self._positions_snapshot()})
            return

        dec = step_decimals_for(sym)[1]
        mid = self.ensure_state(sym)
        spr = spread_for(sym)
        px_exec = mid + (spr/2.0) if side=="buy" else mid - (spr/2.0)
        px_exec = round(px_exec, dec)

        commission = self.commission_for(sym, px_exec*qty)
        self.account["balance"] -= commission

        order_id = self._order_seq; self._order_seq += 1

        if bool(self.account.get("netting_mode", False)):
            self._open_or_update_position(sym, side, qty, px_exec, sl, tp, position_id=order_id)
        else:
            self._create_position(sym, side, qty, px_exec, sl, tp, position_id=order_id)

        # DB write is best-effort — never let it abort the WS response
        try:
            await self._db_mirror_open_or_update(order_id, sym, side.upper(), qty, px_exec, sl, tp,
                                                 commission, bool(self.account.get("netting_mode", False)))
        except Exception as exc:
            log.error("[order_new] DB mirror failed for %s %s: %s", side, sym, exc, exc_info=True)

        await self.send_json({"type":"order_ack","order_id":order_id,"symbol":sym,"side":side,"qty":qty,"status":"accepted"})
        await self.send_json({"type":"order_fill","order_id":order_id,"symbol":sym,"side":side,"qty":qty,"price":px_exec,
                              "commission":commission,"ts":int(datetime.utcnow().timestamp())})

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

        # permite ids temporales "tmp-xxxx"
        if not found and (pid is None or str(pid).startswith("tmp-")):
            last_idx = None
            for i in range(len(self._positions)-1, -1, -1):
                if self._positions[i]["symbol"]==sym:
                    last_idx = i; break
            if last_idx is not None:
                if sl is not None: self._positions[last_idx]["sl"] = float(sl)
                if tp is not None: self._positions[last_idx]["tp"] = float(tp)
                await self._db_mirror_update_sl_tp(self._positions[last_idx]["id"], sym,
                                                   self._positions[last_idx].get("sl"), self._positions[last_idx].get("tp"))
                found = True

        if found:
            await self.send_json({"type":"positions","items":self._positions_snapshot()})
        else:
            await self.send_json({"type":"warn","message":"order_update_not_found"})

    async def _order_close(self, data: dict):
        pid = data.get("id")                          # may arrive as str or int
        sym_hint = data.get("symbol", None)           # optional — client may omit it

        log.info("[close] received pid=%r sym_hint=%r positions_in_memory=%d ids=%s",
                 pid, sym_hint, len(self._positions),
                 [(p.get("id"), p.get("symbol"), p.get("side")) for p in self._positions])

        remaining, closed = [], None

        for p in self._positions:
            # Normalise both sides to str so "5" == 5 works
            id_match  = (pid is not None) and (str(p.get("id")) == str(pid))
            sym_match = (sym_hint is None) or (p.get("symbol") == sym_hint)

            log.debug("[close] checking pos id=%r sym=%r → id_match=%s sym_match=%s",
                      p.get("id"), p.get("symbol"), id_match, sym_match)

            if id_match and sym_match and not closed:
                sym  = p["symbol"]                    # always use the position's own symbol
                dec  = step_decimals_for(sym)[1]
                last = round(self.ensure_state(sym), dec)

                realized = self._realized_pnl_for(p, last)
                self.account["balance"] += realized
                closed = {
                    "id": p["id"], "symbol": sym, "side": p["side"],
                    "qty": p["qty"], "avg": p["avg"],
                    "close_px": last, "reason": "manual",
                    "realized_pnl": realized, "ts": int(time.time()),
                }
                log.info("[close] MATCH found pos id=%r sym=%r side=%r close_px=%s realized=%.4f",
                         p["id"], sym, p["side"], last, realized)
                try:
                    await self._db_mirror_close_position(p, close_px=last, reason="manual", realized_pnl=realized)
                    log.info("[close] _db_mirror_close_position completed OK for pos id=%r", p["id"])
                except Exception as exc:
                    log.error("[close] _db_mirror_close_position FAILED for pos id=%r: %s", p["id"], exc, exc_info=True)
            else:
                remaining.append(p)

        self._positions = remaining
        await self._recalc_account_and_push()

        if closed:
            log.info("[close] order closed OK. remaining positions=%d", len(self._positions))
            await self.send_json({"type":"order_close", **closed})
            await self.send_json({"type":"positions","items":self._positions_snapshot()})
        else:
            log.warning("[close] NO MATCH for pid=%r sym_hint=%r — sending order_close_not_found", pid, sym_hint)
            await self.send_json({"type":"warn","message":"order_close_not_found"})

    # ---------------- Cuenta / PnL ----------------
    def commission_for(self, symbol, notional): return max(0.0, notional*0.0002)
    def min_qty_for(self, symbol): return 0.001 if symbol in ("BTCUSD","ETHUSD") else 0.01

    def _pretrade_check(self, symbol, side, qty):
        if qty < self.min_qty_for(symbol): return False, "min_qty_violation"
        lev = max(1, int(self.account.get("leverage", 1)))
        mid = float(self.ensure_state(symbol))
        est_margin = abs(mid*qty)/lev
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
                                "opened_at":int(datetime.utcnow().timestamp())})

    def _create_position(self, symbol, side, qty, fill_px, sl=None, tp=None, position_id=None):
        dec = step_decimals_for(symbol)[1]
        self._positions.append({"id":position_id or self._order_seq, "symbol":symbol,"side":side,
                                "qty":qty,"avg":round(fill_px,dec),"sl":sl,"tp":tp,
                                "opened_at":int(datetime.utcnow().timestamp())})

    def _positions_snapshot(self): return [dict(p) for p in self._positions]

    def _unrealized_pnl_total(self):
        return sum(self._unrealized_pnl_for(p, self.ensure_state(p["symbol"])) for p in self._positions)

    def _unrealized_pnl_for(self, pos, last_price):
        return (last_price - pos["avg"]) * pos["qty"] if pos["side"]=="buy" else (pos["avg"] - last_price) * pos["qty"]

    def _realized_pnl_for(self, pos, close_price): return self._unrealized_pnl_for(pos, close_price)

    def _margin_used_total(self):
        lev = max(1, int(self.account.get("leverage", 1)))
        total = 0.0
        for p in self._positions:
            notional = abs(self.ensure_state(p["symbol"]) * p["qty"])
            total += notional/lev
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

        await self.send_json({
            "type":"account:update",
            "balance":round(self.account["balance"],2),
            "equity":self.account["equity"],
            "pnl_unreal":self.account["pnl_unreal"],
            "upnl":self.account["pnl_unreal"],
            "margin_used":self.account["margin_used"],
            "free_margin":free_margin,
            "leverage":self.account["leverage"],
            "netting_mode":bool(self.account.get("netting_mode", False)),
        })

    async def _check_tp_sl(self, symbol: str, last_price: float):
        dec = step_decimals_for(symbol)[1]
        remaining, closed = [], []
        now = int(time.time())

        for p in self._positions:
            if p["symbol"] != symbol:
                remaining.append(p); continue

            side = p["side"]; sl = p.get("sl"); tp = p.get("tp")

            trail = p.get("trail_dist")
            if trail and trail > 0:
                if side=="buy":
                    p["best"] = max(p.get("best", p["avg"]), last_price)
                    p["sl"] = round(p["best"] - trail, dec)
                    sl = p["sl"]
                else:
                    p["best"] = min(p.get("best", p["avg"]), last_price)
                    p["sl"] = round(p["best"] + trail, dec)
                    sl = p["sl"]

            sl_hit = sl is not None and ((side=="buy" and last_price<=sl) or (side=="sell" and last_price>=sl))
            tp_hit = tp is not None and ((side=="buy" and last_price>=tp) or (side=="sell" and last_price<=tp))

            if sl_hit or tp_hit:
                realized = self._realized_pnl_for(p, last_price)
                self.account["balance"] += realized
                closed.append({"id":p["id"],"symbol":symbol,"side":side,"qty":p["qty"],"avg":p["avg"],
                               "close_px":round(last_price,dec),"reason":"tp" if tp_hit else "sl",
                               "realized_pnl":realized,"ts":now})
                await self._db_mirror_close_position(p, close_px=last_price, reason=("tp" if tp_hit else "sl"),
                                                     realized_pnl=realized)
            else:
                remaining.append(p)

        if closed:
            self._positions = remaining
            await self._recalc_account_and_push()
            for c in closed: await self.send_json({"type":"order_close", **c})
            await self.send_json({"type":"positions","items":self._positions_snapshot()})

    # ---------------- DB helpers (best-effort) ----------------
    async def send_positions_snapshot(self):
        items = await self._db_fetch_open_positions()
        self._positions = [
            {
                "id": it["id"], "symbol": it["symbol"], "side": it["side"],
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

        self.account["balance"]  = float(acc.get("balance", self.account["balance"]))
        self.account["equity"]   = float(acc.get("equity", self.account["equity"]))
        self.account["leverage"] = int(acc.get("leverage", self.account["leverage"]))
        self.account["netting_mode"] = bool(acc.get("netting_mode", self.account["netting_mode"]))
        log.info("[hydrate] balance=%.2f equity=%.2f leverage=%s",
                 self.account["balance"], self.account["equity"], self.account["leverage"])

        items = await self._db_fetch_open_positions()
        self._positions = []
        for it in items:
            self._positions.append({
                "id":it["id"], "symbol":it["symbol"], "side":it["side"],
                "qty":float(it["qty"]), "avg":float(it["avg_price"]),
                "sl":it.get("sl"), "tp":it.get("tp"),
                "opened_at":it.get("opened_ts", int(time.time())),
            })
        log.info("[hydrate] loaded %d open position(s): %s",
                 len(self._positions), [(p["id"], p["symbol"], p["side"]) for p in self._positions])

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
    def _db_read_account(self, acc_id:int):
        try:
            obj = TradingAccount.objects.get(id=acc_id)
            return {"id":obj.id,"balance":obj.balance,"equity":obj.equity,
                    "leverage":getattr(obj,"leverage",50),"netting_mode":getattr(obj,"netting_mode",False)}
        except TradingAccount.DoesNotExist:
            return None

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
    def _db_sync_account_balances(self):
        if not self._db_account_id: return
        TradingAccount.objects.filter(id=self._db_account_id).update(
            balance=self.account["balance"], equity=self.account["equity"]
        )

    @database_sync_to_async
    def _db_mirror_open_or_update(self, order_id, symbol, side, qty, price, sl, tp, commission, netting_mode):
        if not self._db_account_id: return
        from decimal import Decimal
        with transaction.atomic():
            if commission and commission>0:
                LedgerEntry.objects.create(
                    account_id=self._db_account_id, event_type=LedgerEntry.EV_COMMISSION,
                    amount=Decimal(-abs(commission)), balance_after=Decimal(self.account["balance"]),
                    meta={"symbol":symbol,"side":side,"client_pos_id":order_id},
                )
            if netting_mode:
                pos = Position.objects.select_for_update().filter(
                    account_id=self._db_account_id, symbol=symbol, side=side).first()
                if pos:
                    old_qty = Decimal(pos.qty); old_avg = Decimal(pos.avg_price)
                    new_qty = old_qty + Decimal(qty)
                    new_avg = (old_avg*old_qty + Decimal(price)*Decimal(qty)) / (new_qty if new_qty!=0 else Decimal(1))
                    pos.qty=new_qty; pos.avg_price=new_avg
                    if sl is not None: pos.sl=Decimal(sl)
                    if tp is not None: pos.tp=Decimal(tp)
                    pos.save()
                else:
                    Position.objects.create(
                        account_id=self._db_account_id, symbol=symbol, side=side,
                        qty=Decimal(qty), avg_price=Decimal(price),
                        **({"sl":Decimal(sl)} if sl is not None else {}),
                        **({"tp":Decimal(tp)} if tp is not None else {}),
                        external_id=str(order_id),
                    )
            else:
                Position.objects.create(
                    account_id=self._db_account_id, symbol=symbol, side=side,
                    qty=Decimal(qty), avg_price=Decimal(price),
                    **({"sl":Decimal(sl)} if sl is not None else {}),
                    **({"tp":Decimal(tp)} if tp is not None else {}),
                    external_id=str(order_id),
                )

    @database_sync_to_async
    def _db_mirror_update_sl_tp(self, pos_id, symbol, sl, tp):
        if not self._db_account_id or not pos_id: return
        try:
            pos = Position.objects.get(id=pos_id, account_id=self._db_account_id)
        except Position.DoesNotExist:
            pos = Position.objects.filter(account_id=self._db_account_id, symbol=symbol).order_by("-id").first()
        if not pos: return
        changed=False
        from decimal import Decimal
        if sl is not None: pos.sl = Decimal(sl); changed=True
        if tp is not None: pos.tp = Decimal(tp); changed=True
        if changed: pos.save()

    @database_sync_to_async
    def _db_mirror_close_position(self, pos_mem, close_px, reason, realized_pnl):
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
                pos = Position.objects.filter(
                    account_id=self._db_account_id,
                    symbol=pos_mem["symbol"],
                    side__iexact=pos_mem["side"],
                ).order_by("-id").first()
                if pos:
                    log.info("[db_close] found Position by symbol+side fallback id=%r", pos.id)
                else:
                    log.warning("[db_close] no matching DB Position found — Trade will still be created")

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

                # Risk engine — check violations and update trader score
                from .risk_engine import check_and_enforce_risk, update_trader_score
                violations = check_and_enforce_risk(account)
                if violations:
                    log.warning(
                        "[risk] account #%s suspended: %s",
                        self._db_account_id,
                        [v.violation_type for v in violations],
                    )
                    # Sync suspension back to in-memory state
                    self.account["status"] = "Suspendido"
                update_trader_score(account)

    # ---------------- Util: enviar JSON ----------------
    async def send_json(self, payload: dict):
        await self.send(text_data=json.dumps(payload))